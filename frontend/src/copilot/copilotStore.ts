/**
 * State for the editing copilot.
 *
 * Kept out of `workflowStore` because almost none of it is document state: the
 * conversation, the stage stack, the plan waiting for approval. The document is
 * still the single source of truth; this store only reads and writes it through
 * the actions that already exist (`pushHistory`, `applyLiveGraph`).
 */

import { create } from 'zustand';

import { streamCopilot, type RunStream } from '@/api/client';
import { translate } from '@/i18n';
import { useWorkflow } from '@/store/workflowStore';
import type {
  CopilotApproval,
  CopilotBrief,
  CopilotEvent,
  CopilotMode,
  CopilotOutputReview,
  CopilotPlan,
  CopilotResearch,
  CopilotResult,
  TokenUsage,
  TrialRunResult,
  WorkflowGraph,
} from '@/types/workflow';

export type StageStatus =
  | 'pending'
  | 'running'
  | 'progress'
  | 'done'
  | 'failed'
  | 'skipped'
  | 'disabled'
  | 'unavailable'
  | 'empty';

export interface CopilotStage {
  id: string;
  status: StageStatus;
  detail?: string | null;
}

/**
 * One observable step of the turn.
 *
 * The stage strip shows only the *current* stage, so repeated corrections and
 * trial runs collapsed into a single row. This list keeps every tool call,
 * validation pass and test, in order, so "it thought and edited three times" is
 * visible instead of inferred.
 */
export interface CopilotTraceEntry {
  id: string;
  kind: 'tool' | 'validation' | 'test' | 'correction' | 'review';
  /** Tool name, or the stage that produced the entry. */
  name: string;
  detail?: string;
  status: 'running' | 'ok' | 'failed';
}

export interface CopilotMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  text: string;
  /** Distinguishes a report/error from ordinary prose. */
  kind?: 'report' | 'error' | 'status';
  /**
   * Reasoning that belongs to THIS reply. Kept on the message rather than in a
   * turn-level array so it renders above the answer it explains; a turn-level
   * block is always appended after the messages and therefore always lands
   * below the text, which is backwards.
   */
  thinking?: string[];
}

export interface CopilotOptions {
  sampleInput?: Record<string, unknown> | null;
  expectedOutput?: string;
  /** Inline data URLs, already compressed by the composer. */
  images?: string[];
}

/**
 * One saved conversation.
 *
 * History lives in this browser, not on the server: the copilot transcript can
 * contain the user's business description, and the backend has no account to
 * attach it to. It is per browser by design, not a shared team log.
 */
export interface CopilotConversation {
  id: string;
  title: string;
  at: string;
  graphId: string;
  messages: CopilotMessage[];
  thinking: string[];
}

const HISTORY_KEY = 'scaffold.copilot.history.v1';
const CURRENT_KEY = 'scaffold.copilot.current.v1';

/** Each workflow remembers its own last conversation. */
function currentKey(graphId: string): string {
  return `${CURRENT_KEY}:${graphId}`;
}
const THINKING_KEY = 'scaffold.copilot.thinking.v1';

function readJson<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : fallback;
  } catch {
    return fallback;
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // A private window or disabled storage must not break the conversation.
  }
}

function loadHistory(): CopilotConversation[] {
  const stored = readJson<CopilotConversation[]>(HISTORY_KEY, []);
  return Array.isArray(stored)
    ? stored.filter(
        (entry) =>
          entry &&
          typeof entry.id === 'string' &&
          Array.isArray(entry.messages) &&
          entry.messages.length > 0,
      )
    : [];
}

function titleFor(messages: CopilotMessage[]): string {
  const first = messages.find((entry) => entry.role === 'user' && entry.text.trim());
  return (first?.text.trim() || 'Conversation').slice(0, 80);
}

interface PendingTurn {
  message: string;
  options: CopilotOptions;
  images: string[];
  selection: { nodes: string[]; edges: string[] };
  history: { role: 'user' | 'assistant'; content: string }[];
  thinking: boolean;
}

/**
 * The last few turns, for resolving "make it shorter" / "that node".
 *
 * Reports are useful context but can run long, so each message is clipped and
 * only the recent window survives; the backend applies its own bound too.
 */
function recentHistory(
  messages: CopilotMessage[],
): { role: 'user' | 'assistant'; content: string }[] {
  return messages
    .filter((entry) => entry.role !== 'system' && entry.text.trim())
    .slice(-8)
    .map((entry) => ({
      role: entry.role as 'user' | 'assistant',
      content: entry.text.slice(0, 2000),
    }));
}

interface CopilotState {
  mode: CopilotMode;
  /** True while the canvas is in marquee-selection mode. */
  selecting: boolean;
  running: boolean;
  /** A plan is waiting for the user's approval. */
  waiting: boolean;
  stages: CopilotStage[];
  /** Every tool call, validation and test from the current turn, in order. */
  trace: CopilotTraceEntry[];
  messages: CopilotMessage[];
  /** Reasoning blocks from this turn, rendered as one collapsible trace. */
  thinking: string[];
  /** Ask the backend to explain its decision, not just its output. */
  thinkingEnabled: boolean;
  /** Tokens spent on the current turn, updated as each model reply arrives. */
  usage: TokenUsage | null;
  /** The assistant message currently being streamed, if any. */
  streamingId: string | null;
  conversations: CopilotConversation[];
  historyOpen: boolean;
  activeConversationId: string | null;
  /** The workflow whose conversation is currently in the sidebar. */
  conversationGraphId: string;
  brief: CopilotBrief | null;
  plan: CopilotPlan | null;
  research: CopilotResearch | null;
  approval: CopilotApproval | null;
  result: CopilotResult | null;
  test: TrialRunResult | null;
  outputReview: CopilotOutputReview | null;
  error: string | null;
  stream: RunStream | null;
  /** The canvas before the turn that is allowed to edit. */
  preEdit: WorkflowGraph | null;
  pending: PendingTurn | null;

  setMode: (mode: CopilotMode) => void;
  toggleThinking: () => void;
  toggleHistory: () => void;
  switchWorkflow: (graphId: string) => void;
  loadConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
  setSelecting: (selecting: boolean) => void;
  clearScope: () => void;
  send: (message: string, options?: CopilotOptions) => void;
  approve: () => void;
  discardPlan: () => void;
  stop: () => void;
  revert: () => void;
  reset: () => void;
}

let counter = 0;
function uid(): string {
  counter += 1;
  return `m${counter}`;
}

function message(role: CopilotMessage['role'], text: string, kind?: CopilotMessage['kind']): CopilotMessage {
  return { id: uid(), role, text, kind };
}

/**
 * Move reasoning saved by the old turn-level format onto its reply.
 *
 * Older conversations stored one `thinking` array per conversation. Loading
 * one must not keep the old bug (reasoning below the answer) alive; attaching
 * it to the last assistant message makes history render the same way new
 * replies do. If there is no assistant message, it stays at turn level and the
 * fallback block shows it.
 */
function withLegacyThinking(
  messages: CopilotMessage[],
  thinking: string[],
): CopilotMessage[] {
  if (!thinking.length || messages.some((entry) => entry.thinking?.length)) {
    return messages;
  }
  let target = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index]?.role === 'assistant') {
      target = index;
      break;
    }
  }
  if (target === -1) return messages;
  return messages.map((entry, index) =>
    index === target ? { ...entry, thinking } : entry,
  );
}

export const useCopilot = create<CopilotState>((set, get) => {
  const storedHistory = loadHistory();

  /**
   * Upsert the active conversation. Called after a user turn and after the
   * assistant's reply, so a reload mid-conversation keeps both sides.
   */
  function persistConversation(
    messages: CopilotMessage[],
    thinking: string[],
    currentId: string | null,
  ): string | null {
    const visible = messages.filter((entry) => entry.role !== 'system');
    if (visible.length === 0) return currentId;

    const id = currentId ?? `c${Date.now()}`;
    const graphId = useWorkflow.getState().graphId;
    const entry: CopilotConversation = {
      id,
      title: titleFor(visible),
      at: new Date().toISOString(),
      graphId,
      messages: visible,
      thinking,
    };
    const next = [entry, ...get().conversations.filter((item) => item.id !== id)].slice(
      0,
      20,
    );
    writeJson(HISTORY_KEY, next);
    if (graphId) writeJson(currentKey(graphId), id);
    set({
      conversations: next,
      activeConversationId: id,
      conversationGraphId: graphId,
    });
    return id;
  }

  function updateStage(event: CopilotEvent): void {
    if (!event.stage) return;
    const status = (event.status ?? 'running') as StageStatus;
    set((state) => {
      const existing = state.stages.findIndex((stage) => stage.id === event.stage);
      const next = [...state.stages];
      if (existing === -1) {
        next.push({ id: event.stage!, status, detail: event.detail });
      } else {
        next[existing] = { id: event.stage!, status, detail: event.detail };
      }
      return { stages: next };
    });
  }

  function addTrace(entry: Omit<CopilotTraceEntry, 'id'>): void {
    set((state) => ({
      trace: [...state.trace, { ...entry, id: `t${uid()}` }],
    }));
  }

  function resolveTrace(
    kind: CopilotTraceEntry['kind'],
    name: string,
    status: CopilotTraceEntry['status'],
    detail?: string,
  ): void {
    set((state) => {
      const index = [...state.trace]
        .reverse()
        .findIndex(
          (entry) =>
            entry.kind === kind &&
            entry.name === name &&
            entry.status === 'running',
        );
      if (index === -1) return state;
      const at = state.trace.length - 1 - index;
      const current = state.trace[at];
      if (!current) return state;
      const next = [...state.trace];
      next[at] = { ...current, status, detail: detail ?? current.detail };
      return { trace: next };
    });
  }

  function compact(value: unknown, limit = 180): string {
    try {
      const text = typeof value === 'string' ? value : JSON.stringify(value);
      return text && text.length > limit ? `${text.slice(0, limit)}…` : text ?? '';
    } catch {
      return String(value);
    }
  }

  function handle(event: CopilotEvent): void {
    switch (event.type) {
      case 'stage':
        updateStage(event);
        break;
      case 'tool':
        addTrace({
          kind: 'tool',
          name: event.tool ?? 'tool',
          detail: compact(event.arguments ?? {}),
          status: 'running',
        });
        break;
      case 'tool_result':
        resolveTrace(
          'tool',
          event.tool ?? 'tool',
          event.ok === false ? 'failed' : 'ok',
          compact(event.result ?? '', 240),
        );
        break;
      case 'validation': {
        const errors = event.errors ?? [];
        addTrace({
          kind: 'validation',
          name: event.ok === false ? 'validation_failed' : 'validation_passed',
          detail: errors.length
            ? compact(
                errors
                  .slice(0, 4)
                  .map((issue) => `${issue.code}: ${issue.message}`)
                  .join('\n'),
                300,
              )
            : undefined,
          status: event.ok === false ? 'failed' : 'ok',
        });
        break;
      }
      case 'self_test': {
        if (event.phase === 'start') {
          addTrace({ kind: 'test', name: 'self_test', status: 'running' });
        } else if (event.phase === 'done') {
          const trial = event.result as TrialRunResult | undefined;
          if (trial) {
            set({ test: trial });
            // The Run tab reads this slot; it is not a RunManager run, so it
            // cannot come from /runs/history.
            useWorkflow.getState().setSelfTest(trial);
          }
          resolveTrace(
            'test',
            'self_test',
            trial && trial.status === 'failed' ? 'failed' : 'ok',
            trial?.summary,
          );
        }
        break;
      }
      case 'correcting':
        addTrace({
          kind: 'correction',
          name: event.kind ?? 'correction',
          detail: [event.failed_node, event.message]
            .filter(Boolean)
            .join(' · '),
          status: 'running',
        });
        break;
      case 'output_review': {
        const review = event.review;
        if (review) set({ outputReview: review });
        addTrace({
          kind: 'review',
          name: 'output_review',
          detail: review?.reason,
          status:
            review?.matches === false
              ? 'failed'
              : review?.matches
                ? 'ok'
                : 'running',
        });
        break;
      }
      case 'thinking': {
        const text = event.text;
        if (text) {
          set((state) => {
            const blocks = [...state.thinking];
            if (event.append && blocks.length > 0) {
              blocks[blocks.length - 1] += text;
            } else {
              blocks.push(text);
            }

            // Attach the reasoning to the reply it belongs to. The first
            // thinking event opens the assistant bubble (answer tokens append
            // into the same one), so the block renders above the answer rather
            // than after every message in the transcript.
            const streamingId = state.streamingId;
            if (streamingId) {
              return {
                thinking: blocks,
                messages: state.messages.map((entry) =>
                  entry.id === streamingId ? { ...entry, thinking: blocks } : entry,
                ),
              };
            }
            const id = uid();
            return {
              thinking: blocks,
              streamingId: id,
              messages: [
                ...state.messages,
                {
                  id,
                  role: 'assistant' as const,
                  text: '',
                  thinking: blocks,
                },
              ],
            };
          });
        }
        break;
      }
      case 'token': {
        const text = event.text ?? '';
        if (!text) break;
        const streamingId = get().streamingId;
        if (streamingId) {
          set((state) => ({
            messages: state.messages.map((entry) =>
              entry.id === streamingId
                ? { ...entry, text: entry.text + text }
                : entry,
            ),
          }));
        } else {
          const id = uid();
          set((state) => ({
            streamingId: id,
            messages: [
              ...state.messages,
              { id, role: 'assistant' as const, text },
            ],
          }));
        }
        break;
      }
      case 'usage':
        set({ usage: event.total ?? event.usage ?? get().usage });
        break;
      case 'brief':
        if (event.brief) set({ brief: event.brief });
        break;
      case 'plan':
        if (event.plan) set({ plan: event.plan });
        break;
      case 'approval':
        if (event.brief && event.plan) {
          set({
            approval: {
              brief: event.brief,
              plan: event.plan,
              research: event.research,
            },
            brief: event.brief,
            plan: event.plan,
            research: event.research ?? get().research,
            waiting: true,
            running: false,
            stream: null,
            streamingId: null,
            // The plan is on screen; a running "designing the plan" strip below
            // it is stale state, not history worth keeping.
            stages: [],
          });
          {
            const nextMessages = [
              ...get().messages,
              message('assistant', translate('copilot.planReady'), 'status'),
            ];
            const activeId = persistConversation(
              nextMessages,
              get().thinking,
              get().activeConversationId,
            );
            set({ messages: nextMessages, activeConversationId: activeId });
          }
        }
        break;
      case 'graph':
        if (event.graph) useWorkflow.getState().applyLiveGraph(event.graph);
        break;
      case 'done': {
        const result = (event as CopilotEvent & { result?: CopilotResult }).result;
        if (!result) break;
        set({
          result,
          brief: result.brief ?? get().brief,
          plan: result.plan ?? get().plan,
          research: result.research ?? get().research,
          test: result.test ?? null,
          outputReview: result.output_review ?? null,
          usage: result.usage ?? get().usage,
          running: false,
          waiting: false,
          stream: null,
        });

        // A conversation leaves no pipeline behind. The stage strip is for an
        // edit in progress; keeping "designing the plan" visible after a hello
        // made it look like leftover UI.
        const kind = result.brief?.request_kind;
        if (kind === 'chat' || kind === 'question') {
          set({ stages: [] });
        }

        if (result.needs_approval) {
          // The approval event and the done event can arrive in either order
          // relative to a fast client read. Rebuild the gate from the result so
          // a missed event cannot leave the plan card hidden while the server
          // is waiting for an answer.
          set({
            waiting: true,
            streamingId: null,
            stages: [],
            approval: {
              brief: result.brief,
              plan: result.plan,
              research: result.research,
            },
          });
          break;
        }

        if (result.rollback) {
          set({ streamingId: null });
          const before = get().preEdit;
          if (before) useWorkflow.getState().applyLiveGraph(before);
          set((state) => ({
            messages: [
              ...state.messages,
              message('assistant', translate('copilot.rollback'), 'error'),
            ],
          }));
          break;
        }

        if (result.applied && result.graph) {
          useWorkflow.getState().applyLiveGraph(result.graph);
          useWorkflow.getState().validate();
          useWorkflow.getState().setAiChange({
            request: get().pending?.message ?? '',
            mode: result.mode,
            brief: result.brief,
            plan: result.plan,
            summary: result.summary,
            changes: result.changes,
            test: result.test ?? null,
            output_review: result.output_review ?? null,
            applied: result.applied,
            rollback: result.rollback,
            at: new Date().toISOString(),
          });
        }

        {
          const streamingId = get().streamingId;
          const nextMessages = streamingId
            ? get().messages.map((entry) =>
                entry.id === streamingId
                  ? {
                      ...entry,
                      text: reportText(result),
                      kind:
                        kind === 'edit'
                          ? ('report' as const)
                          : undefined,
                    }
                  : entry,
              )
            : [
                ...get().messages,
                {
                  ...message('assistant', reportText(result), 'report'),
                  thinking: get().thinking.length ? get().thinking : undefined,
                },
              ];
          const activeId = persistConversation(
            nextMessages,
            get().thinking,
            get().activeConversationId,
          );
          set({
            messages: nextMessages,
            activeConversationId: activeId,
            streamingId: null,
          });
        }
        break;
      }
      case 'error': {
        // A failed turn must not leave a half-edited canvas. The pre-edit
        // snapshot is the same one Undo would restore, applied silently.
        const before = get().preEdit;
        if (before) {
          useWorkflow.getState().applyLiveGraph(before);
          useWorkflow.getState().validate();
        }
        set({
          running: false,
          stream: null,
          streamingId: null,
          error: event.message ?? 'AI request failed.',
        });
        {
          const nextMessages = [
            ...get().messages,
            {
              ...message(
                'assistant',
                translate('copilot.errorPrefix', { message: event.message ?? '' }),
                'error',
              ),
              thinking: get().thinking.length ? get().thinking : undefined,
            },
          ];
          const activeId = persistConversation(
            nextMessages,
            get().thinking,
            get().activeConversationId,
          );
          set({ messages: nextMessages, activeConversationId: activeId });
        }
        break;
      }
      default:
        break;
    }
  }

  function start(
    body: {
      message: string;
      graph: WorkflowGraph;
      mode: CopilotMode;
      selection: { nodes: string[]; edges: string[] };
      sampleInput?: Record<string, unknown> | null;
      expectedOutput?: string;
      approval?: CopilotApproval | null;
      history?: { role: 'user' | 'assistant'; content: string }[];
      thinking?: boolean;
      images?: string[];
    },
    options: { pushHistory: boolean },
  ): void {
    if (options.pushHistory) {
      useWorkflow.getState().pushHistory();
      set({ preEdit: body.graph });
    } else {
      set({ preEdit: null });
    }

    set({ running: true, waiting: false, error: null, approval: null });

    const stream = streamCopilot(body, {
      onEvent: handle,
      onClose: () => {
        if (get().running) set({ running: false, stream: null });
      },
      onError: (error) => {
        set({ streamingId: null });
        const before = get().preEdit;
        if (before) {
          useWorkflow.getState().applyLiveGraph(before);
          useWorkflow.getState().validate();
        }
        set((state) => ({
          running: false,
          stream: null,
          error: error.message,
          messages: [
            ...state.messages,
            message(
              'assistant',
              translate('copilot.errorPrefix', { message: error.message }),
              'error',
            ),
          ],
        }));
      },
    });
    set({ stream });
  }

  return {
    mode: 'confirm',
    selecting: false,
    running: false,
    waiting: false,
    stages: [],
    trace: [],
    messages: [],
    thinking: [],
    thinkingEnabled: readJson<boolean>(THINKING_KEY, false),
    usage: null,
    streamingId: null,
    conversations: storedHistory,
    historyOpen: false,
    activeConversationId: null,
    conversationGraphId: '',
    brief: null,
    plan: null,
    research: null,
    approval: null,
    result: null,
    test: null,
    outputReview: null,
    error: null,
    stream: null,
    preEdit: null,
    pending: null,

    setMode: (mode) => set({ mode, approval: null, waiting: false }),
    toggleThinking: () => {
      const enabled = !get().thinkingEnabled;
      writeJson(THINKING_KEY, enabled);
      set({ thinkingEnabled: enabled });
    },
    toggleHistory: () => set({ historyOpen: !get().historyOpen }),
    switchWorkflow: (graphId) => {
      if (!graphId || graphId === get().conversationGraphId) return;
      // Events from the old workflow's stream must not land on the new canvas.
      get().stream?.close();
      const pointer = readJson<string | null>(currentKey(graphId), null);
      const entry =
        get().conversations.find(
          (item) => item.id === pointer && item.graphId === graphId,
        ) ?? null;
      set({
        conversationGraphId: graphId,
        activeConversationId: entry?.id ?? null,
        messages: entry
          ? withLegacyThinking(
              entry.messages.map((item) => ({ ...item, id: uid() })),
              entry.thinking,
            )
          : [],
        thinking: entry?.thinking ?? [],
        stages: [],
        trace: [],
        thinkingEnabled: get().thinkingEnabled,
        usage: null,
        streamingId: null,
        running: false,
        waiting: false,
        stream: null,
        historyOpen: false,
        brief: null,
        plan: null,
        research: null,
        approval: null,
        result: null,
        test: null,
        outputReview: null,
        error: null,
        pending: null,
        preEdit: null,
      });
    },
    loadConversation: (id) => {
      const entry = get().conversations.find((item) => item.id === id);
      if (!entry) return;
      writeJson(currentKey(entry.graphId), id);
      set({
        messages: withLegacyThinking(
          entry.messages.map((item) => ({ ...item, id: uid() })),
          entry.thinking,
        ),
        thinking: entry.thinking,
        activeConversationId: id,
        historyOpen: false,
        streamingId: null,
        stages: [],
        trace: [],
        thinkingEnabled: get().thinkingEnabled,
        brief: null,
        plan: null,
        research: null,
        approval: null,
        result: null,
        test: null,
        outputReview: null,
        error: null,
        pending: null,
        preEdit: null,
      });
    },
    deleteConversation: (id) => {
      const next = get().conversations.filter((item) => item.id !== id);
      writeJson(HISTORY_KEY, next);
      if (get().activeConversationId === id) {
        const entry = get().conversations.find((item) => item.id === id);
        if (entry?.graphId) writeJson(currentKey(entry.graphId), null);
        set({
          conversations: next,
          messages: [],
          thinking: [],
          trace: [],
          activeConversationId: null,
          result: null,
          brief: null,
          plan: null,
          research: null,
          approval: null,
          test: null,
          outputReview: null,
        });
      } else {
        set({ conversations: next });
      }
    },
    setSelecting: (selecting) => set({ selecting }),
    clearScope: () => {
      useWorkflow.getState().setCopilotSelection([], []);
    },

    send: (text, options = {}) => {
      const images = options.images ?? [];
      const trimmed = text.trim();
      if ((!trimmed && images.length === 0) || get().running) return;
      const messageText = trimmed || translate('copilot.imageOnly');

      const workflow = useWorkflow.getState();
      const selection = workflow.copilotSelection;
      const graph = workflow.toGraph();
      const mode = get().mode;
      const history = recentHistory(get().messages);
      const thinking = get().thinkingEnabled;
      const pending: PendingTurn = {
        message: messageText,
        options,
        images,
        selection,
        history,
        thinking,
      };
      // Before the old turn-level reasoning is cleared, bake it into its reply
      // so starting a new turn does not make history lose its reasoning.
      const nextMessages = [
        ...withLegacyThinking(get().messages, get().thinking),
        message('user', messageText),
      ];
      const activeId = persistConversation(
        nextMessages,
        [],
        get().activeConversationId,
      );
      useWorkflow.getState().setSelfTest(null);

      set({
        // The stage stack above the canvas is the progress indicator. A
        // "thinking" bubble here would linger after the turn and read as a
        // message the assistant never finished.
        messages: nextMessages,
        stages: [],
        trace: [],
        thinking: [],
        activeConversationId: activeId,
        historyOpen: false,
        usage: null,
        streamingId: null,
        brief: null,
        plan: null,
        research: null,
        approval: null,
        result: null,
        test: null,
        outputReview: null,
        error: null,
        pending,
      });

      start(
        {
          message: messageText,
          graph,
          mode,
          selection,
          sampleInput: options.sampleInput ?? null,
          expectedOutput: options.expectedOutput ?? '',
          thinking,
          history,
          images,
        },
        // Autopilot may start editing immediately. Confirm mode only edits after
        // approval, so its first pass must not put an undo entry on the stack.
        { pushHistory: mode === 'auto' },
      );
    },

    approve: () => {
      const state = get();
      if (!state.approval || !state.pending || state.running) return;

      const workflow = useWorkflow.getState();
      const graph = workflow.toGraph();
      const {
        message: text,
        options,
        images,
        selection,
        history,
        thinking,
      } = state.pending;

      set({
        waiting: false,
        approval: null,
        result: null,
        usage: null,
        streamingId: null,
      });

      start(
        {
          message: text,
          graph,
          mode: 'confirm',
          selection,
          sampleInput: options.sampleInput ?? null,
          expectedOutput: options.expectedOutput ?? '',
          approval: state.approval,
          history,
          thinking,
          images,
        },
        { pushHistory: true },
      );
    },

    discardPlan: () => {
      set((state) => ({
        approval: null,
        waiting: false,
        plan: null,
        messages: [
          ...state.messages,
          message('assistant', translate('copilot.discarded'), 'status'),
        ],
      }));
    },

    stop: () => {
      get().stream?.close();
      set({ running: false, stream: null });
    },

    revert: () => {
      const before = get().preEdit;
      if (!before) return;
      useWorkflow.getState().applyLiveGraph(before);
      useWorkflow.getState().validate();
      set({ result: null, outputReview: null });
    },

    reset: () => {
      get().stream?.close();
      // The old transcript stays in history; this starts a fresh conversation.
      const graphId = get().conversationGraphId;
      if (graphId) writeJson(currentKey(graphId), null);
      set({
        running: false,
        waiting: false,
        selecting: false,
        stages: [],
        trace: [],
        messages: [],
        thinking: [],
        activeConversationId: null,
        historyOpen: false,
        usage: null,
        streamingId: null,
        brief: null,
        plan: null,
        research: null,
        approval: null,
        result: null,
        test: null,
        outputReview: null,
        error: null,
        stream: null,
        preEdit: null,
        pending: null,
      });
    },
  };
});

// The sidebar follows the workflow the canvas has open. Switching workflows
// swaps in that workflow's last conversation instead of showing a shared list.
useWorkflow.subscribe((state, previous) => {
  if (state.graphId !== previous.graphId) {
    useCopilot.getState().switchWorkflow(state.graphId);
  }
});

// The workflow may already be loaded before this module is first imported.
{
  const initialGraphId = useWorkflow.getState().graphId;
  if (initialGraphId) useCopilot.getState().switchWorkflow(initialGraphId);
}

function reportText(result: CopilotResult): string {
  const nodeCount =
    result.changes.added_nodes.length +
    result.changes.removed_nodes.length +
    result.changes.updated_nodes.length;
  const edgeCount =
    result.changes.added_edges.length +
    result.changes.removed_edges.length +
    result.changes.updated_edges.length;

  if (!result.applied && nodeCount === 0 && edgeCount === 0) {
    return result.summary || translate('copilot.unchanged');
  }
  return `${result.summary}${
    result.summary ? '\n' : ''
  }${translate('copilot.changed', { nodes: nodeCount, edges: edgeCount })}`;
}
