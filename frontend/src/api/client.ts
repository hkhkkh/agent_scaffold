/**
 * HTTP client and SSE helpers.
 *
 * Uses same-origin `/api` paths, which Vite proxies to the backend in
 * development. That keeps CORS, cookies, and credentials out of the picture.
 */

import { useLocale, type Locale } from '@/i18n';
import { dispatchGateRequired, isGateRequired } from '@/access/accessStore';
import type {
  AIStatus,
  CodeToNodeResult,
  CopilotApproval,
  CopilotEvent,
  CopilotMode,
  GenerationEvent,
  GraphGenerationResult,
  GraphRepairResult,
  NodeFillSuggestion,
  NodeToCodeResult,
  NodeTypesResponse,
  NodeSpec,
  PortType,
  RunEvent,
  RunSummary,
  StateFieldOptions,
  ToolSummary,
  ValidationReport,
  WorkflowGraph,
  WorkflowVersionSummary,
} from '@/types/workflow';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly detail?: unknown,
    readonly code?: string,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

/**
 * Extract a readable message from a FastAPI error body.
 *
 * FastAPI nests validation errors in `detail` as an array of objects; showing
 * that verbatim is unhelpful, so the field and reason are pulled out.
 */
function describeError(status: number, body: unknown): string {
  if (typeof body === 'string' && body.trim()) return body;

  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === 'string') return detail;

  if (Array.isArray(detail)) {
    const parts = detail.slice(0, 4).map((item) => {
      const entry = item as { loc?: unknown[]; msg?: string };
      const field = Array.isArray(entry.loc)
        ? entry.loc.filter((part) => typeof part !== 'number').join('.')
        : '';
      return field ? `${field}: ${entry.msg ?? 'invalid'}` : (entry.msg ?? 'invalid');
    });
    return parts.join('; ');
  }

  if (status === 502 || status === 503) {
    return 'The backend could not complete the AI request. Check the model configuration in Settings.';
  }
  return `Request failed with status ${status}.`;
}

async function request<T>(
  path: string,
  init?: RequestInit & { timeoutMs?: number },
): Promise<T> {
  const { timeoutMs, ...rest } = init ?? {};

  // A long AI call must not be cancelled by a short default timeout, so the
  // caller passes an explicit budget where it matters.
  const controller = timeoutMs ? new AbortController() : null;
  const timer = controller
    ? window.setTimeout(() => controller.abort(), timeoutMs)
    : null;

  try {
    const response = await fetch(`/api${path}`, {
      ...rest,
      credentials: 'same-origin',
      signal: controller?.signal ?? rest.signal,
      headers: {
        'X-Scaffold-Client': 'web',
        ...(rest.body != null ? { 'Content-Type': 'application/json' } : {}),
        ...(rest.headers ?? {}),
      },
    });

    if (response.status === 204) return undefined as T;

    const text = await response.text();
    const parsed: unknown = text ? safeJson(text) : null;
    const code = (parsed as { code?: unknown } | null)?.code;

    if (!response.ok) {
      if (isGateRequired(parsed)) dispatchGateRequired();
      throw new ApiError(
        describeError(response.status, parsed),
        response.status,
        parsed,
        typeof code === 'string' ? code : undefined,
      );
    }
    return parsed as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new ApiError('The request timed out.', 408);
    }
    throw new ApiError(
      error instanceof Error ? error.message : 'Network request failed.',
      0,
    );
  } finally {
    if (timer) window.clearTimeout(timer);
  }
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

// ---------------------------------------------------------------------------
// Catalogue
// ---------------------------------------------------------------------------

export interface ResolvedPorts {
  inputs: { id: string; label: string; type: PortType; required?: boolean }[];
  outputs: { id: string; label: string; type: PortType }[];
}

/**
 * Runtime settings as the backend reports them.
 *
 * `llm.api_key` is the literal `"***"` when a key is configured and `""` when
 * none is — the real credential never leaves the server. Writing `"***"` back
 * is understood by the backend as "keep the stored key".
 */
export interface RuntimeSettings {
  llm: { base_url: string; model: string; api_key: string; temperature: number };
  mock_mode: boolean;
  recursion_limit: number;
  code_node_enabled: boolean;
  ai_enabled: boolean;
}

/**
 * One table in the results database, as `GET /lab/db` reports it.
 *
 * `preview` carries whole rows including the `_row_id` / `_inserted_at`
 * bookkeeping columns the writer adds, so the panel can order and time-stamp
 * what it shows without keeping a clock of its own.
 */
export interface LabTable {
  name: string;
  rows: number;
  columns: string[];
  preview: Record<string, unknown>[];
}

export interface LabDatabase {
  exists: boolean;
  path: string;
  tables: LabTable[];
  total_rows: number;
}

export const api = {
  /**
   * Node type catalogue, localised.
   *
   * Defaults to the active locale so that switching language only needs a
   * re-fetch — no call site has to pass the language through. Node, port, and
   * param *ids* are language-independent, so a workflow saved in one language
   * loads correctly in the other.
   */
  nodeTypes: (lang: Locale = useLocale.getState().locale) =>
    request<NodeTypesResponse>(`/node-types?lang=${lang}`),

  tools: (lang: Locale = useLocale.getState().locale) =>
    request<{ count: number; tools: ToolSummary[] }>(`/tools?lang=${lang}`),

  stateFields: (lang: Locale = useLocale.getState().locale) =>
    request<StateFieldOptions>(`/state-fields?lang=${lang}`),

  /**
   * Resolve a node instance's ports.
   *
   * Needed because a Condition node's handles depend on its configured branches —
   * deriving them on the client would mean duplicating backend logic that would
   * then drift.
   */
  resolvePorts: (node: NodeSpec) =>
    request<ResolvedPorts>('/workflows/ports', {
      method: 'POST',
      body: JSON.stringify({ node }),
    }),

  // -------------------------------------------------------------------------
  // Workflows
  // -------------------------------------------------------------------------

  listWorkflows: () =>
    request<{
      workflows: {
        id: string;
        name: string;
        description: string;
        version: number;
        updated_at: string;
        node_count: number;
        read_only?: boolean;
      }[];
    }>('/workflows'),

  getWorkflow: (id: string) => request<WorkflowGraph>(`/workflows/${id}`),

  createWorkflow: (graph: WorkflowGraph) =>
    request<WorkflowGraph>('/workflows', {
      method: 'POST',
      body: JSON.stringify(graph),
    }),

  saveWorkflow: (graph: WorkflowGraph) =>
    request<WorkflowGraph>(`/workflows/${graph.id}`, {
      method: 'PUT',
      body: JSON.stringify(graph),
    }),

  /**
   * Save the canvas and attach the copilot record to the new version.
   *
   * The record is metadata on the version row, not part of the graph: it must
   * not change the definition hash, the runtime, or an exported agent.
   */
  saveWorkflowWithChange: (graph: WorkflowGraph, change: Record<string, unknown>) =>
    request<WorkflowGraph>(`/workflows/${graph.id}/ai`, {
      method: 'PUT',
      body: JSON.stringify({ graph, change }),
    }),

  listWorkflowVersions: (id: string) =>
    request<{ items: WorkflowVersionSummary[] }>(`/workflows/${id}/versions`),

  getWorkflowVersion: (id: string, version: number) =>
    request<WorkflowGraph>(`/workflows/${id}/versions/${version}`),

  deleteWorkflow: (id: string) =>
    request<void>(`/workflows/${id}`, { method: 'DELETE' }),

  validate: (
    graph: WorkflowGraph,
    lang: Locale = useLocale.getState().locale,
  ) =>
    request<ValidationReport>(`/workflows/validate?lang=${lang}`, {
      method: 'POST',
      body: JSON.stringify(graph),
    }),

  /**
   * Re-arrange a graph's node positions server-side.
   *
   * The layout rule lives with generation so both produce the same picture;
   * the response is an ordinary graph the caller applies like any other edit.
   */
  tidyLayout: (graph: WorkflowGraph, perBand = 6) =>
    request<{ graph: WorkflowGraph }>(
      `/workflows/layout?per_band=${perBand}`,
      {
        method: 'POST',
        body: JSON.stringify(graph),
      },
    ),

  exportWorkflow: (id: string) =>
    request<{ path: string }>(`/workflows/${id}/export`, { method: 'POST' }),

  importWorkflow: (payload: unknown) =>
    request<WorkflowGraph>('/workflows/import', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  // -------------------------------------------------------------------------
  // Runs
  // -------------------------------------------------------------------------

  startRun: (body: {
    graph: WorkflowGraph;
    inputs?: Record<string, unknown>;
    tweaks?: Record<string, Record<string, unknown>>;
  }) =>
    request<RunSummary>('/runs/adhoc', {
      method: 'POST',
      body: JSON.stringify({
        graph: body.graph,
        inputs: body.inputs ?? {},
        tweaks: body.tweaks ?? {},
      }),
    }),

  startSavedRun: (body: {
    workflow_id: string;
    inputs?: Record<string, unknown>;
    tweaks?: Record<string, Record<string, unknown>>;
  }) =>
    request<RunSummary>('/runs', {
      method: 'POST',
      body: JSON.stringify({
        workflow_id: body.workflow_id,
        inputs: body.inputs ?? {},
        tweaks: body.tweaks ?? {},
      }),
    }),

  getRun: (id: string) => request<RunSummary>(`/runs/${id}`),

  getRunSteps: (id: string) =>
    request<{ steps: RunSummary['steps'] }>(`/runs/${id}/steps`),

  // Persisted runs, from the database — as opposed to `GET /runs`, which returns
  // only what is still in memory. That list is a shrinking window: a finished
  // run is evicted after a retention period, so it cannot answer "what did I
  // run last week". Task 16 is what made the difference matter.
  getRunHistory: (workflowId?: string, limit = 50) => {
    const query = new URLSearchParams({ limit: String(limit) });
    if (workflowId) query.set('workflow_id', workflowId);
    return request<{ runs: RunSummary[] }>(`/runs/history?${query}`);
  },

  resumeRun: (id: string, payload: Record<string, unknown>) =>
    request<RunSummary>(`/runs/${id}/resume`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),

  cancelRun: (id: string) => request<RunSummary>(`/runs/${id}/cancel`, { method: 'POST' }),

  testNode: (body: {
    workflow_id: string;
    node_id: string;
    /**
     * The node as the canvas has it. Sent because the unsaved node is the one
     * being tested: a node just added is not in the stored workflow at all, and
     * an edit that has not been saved would be silently ignored.
     */
    node?: Record<string, unknown>;
    inputs?: Record<string, unknown>;
    config_overrides?: Record<string, unknown>;
  }) =>
    request<{
      ok: boolean;
      outputs?: Record<string, unknown>;
      config?: Record<string, unknown>;
      logs: { message: string; level: string }[];
      error?: string;
    }>('/runs/node-test', { method: 'POST', body: JSON.stringify(body), timeoutMs: 180_000 }),

  // -------------------------------------------------------------------------
  // Results database
  // -------------------------------------------------------------------------

  /**
   * A snapshot of the workflow-facing results database.
   *
   * Polled rather than streamed, and read-only. The writer is whichever run
   * happens to be writing rows — not necessarily the one in the foreground —
   * and the panel has to be correct after a run finishes and after a reload,
   * which a snapshot is and a stream is not.
   */
  labDb: (preview = 8) => request<LabDatabase>(`/lab/db?preview=${preview}`),

  // -------------------------------------------------------------------------
  // AI
  // -------------------------------------------------------------------------

  aiStatus: () => request<AIStatus>('/ai/status'),

  fillNode: (body: { graph: WorkflowGraph; node_id: string; instruction?: string }) =>
    request<NodeFillSuggestion>('/ai/fill-node', {
      method: 'POST',
      body: JSON.stringify({ ...body, instruction: body.instruction ?? '' }),
      timeoutMs: 180_000,
    }),

  /**
   * Ask the AI to draft a whole workflow.
   *
   * The budget is long on purpose. Generation is not one model call: a large
   * graph is dozens of tool-calling rounds, and since the model now checks its
   * own wiring against the validator, add up to three more. At five minutes the
   * client gave up while the server was still working — the request had to be
   * re-sent, and the second attempt cost the same. Fifteen minutes is not a
   * promise that it will take that long; it is a promise not to hang up on
   * something that is still making progress.
   */
  generateGraph: (body: { prompt: string; graph?: WorkflowGraph }) =>
    request<GraphGenerationResult>('/ai/generate-graph', {
      method: 'POST',
      body: JSON.stringify(body),
      timeoutMs: 900_000,
    }),

  /**
   * Ask the AI to clear the validator's complaints.
   *
   * `lang` is sent explicitly rather than left to the query string: the backend
   * uses it for the summary it writes back *and* for the language any new node
   * descriptions should be in.
   */
  repairGraph: (
    body: {
      graph: WorkflowGraph;
      severity?: 'error' | 'all';
      instruction?: string;
      max_iterations?: number;
    },
    lang: Locale = useLocale.getState().locale,
  ) =>
    request<GraphRepairResult>('/ai/repair-graph', {
      method: 'POST',
      body: JSON.stringify({ ...body, lang }),
      timeoutMs: 300_000,
    }),

  nodeToCode: (body: { graph: WorkflowGraph; node_id: string }) =>
    request<NodeToCodeResult>('/ai/node-to-code', {
      method: 'POST',
      body: JSON.stringify(body),
      timeoutMs: 180_000,
    }),

  codeToNode: (body: {
    graph: WorkflowGraph;
    node_id: string;
    code: string;
    target_type: string;
  }) =>
    request<CodeToNodeResult>('/ai/code-to-node', {
      method: 'POST',
      body: JSON.stringify(body),
      timeoutMs: 180_000,
    }),

  suggestName: (graph: WorkflowGraph) =>
    request<{ name: string }>('/ai/suggest-name', {
      method: 'POST',
      body: JSON.stringify({ graph }),
      timeoutMs: 60_000,
    }),

  // -------------------------------------------------------------------------
  // Settings
  // -------------------------------------------------------------------------

  settings: () => request<RuntimeSettings>('/settings'),

  updateLLM: (body: {
    base_url: string;
    model: string;
    api_key?: string;
    temperature?: number;
  }) =>
    request<RuntimeSettings>('/settings/llm', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  listModels: () =>
    request<{ models: string[]; current: string }>('/settings/llm/models', {
      timeoutMs: 30_000,
    }),

  testLLM: () =>
    request<{
      ok: boolean;
      model?: string;
      reply?: string;
      detail?: string;
      hint?: string;
      mock_mode?: boolean;
      status?: number;
    }>('/settings/llm/test', { method: 'POST', timeoutMs: 60_000 }),
};

// ---------------------------------------------------------------------------
// Server-Sent Events
// ---------------------------------------------------------------------------

export interface RunStream {
  close: () => void;
}

export interface SseHandlers<T> {
  onEvent: (event: T) => void;
  onError?: (error: Error) => void;
  onClose?: () => void;
}

/**
 * Consume a `text/event-stream` response.
 *
 * Native `EventSource` cannot set headers, read a response body directly, or
 * issue a POST, so the wire format is parsed by hand from a `fetch` stream.
 * Every SSE caller in this client goes through here: the run stream and the
 * generation stream differ only in their URL, method and event type.
 */
function streamSse<T>(
  path: string,
  init: RequestInit,
  handlers: SseHandlers<T>,
): RunStream {
  const controller = new AbortController();
  let closed = false;

  const run = async () => {
    try {
      const headers = new Headers(init.headers);
      headers.set('Accept', 'text/event-stream');
      headers.set('X-Scaffold-Client', 'web');

      const response = await fetch(path, {
        ...init,
        credentials: 'same-origin',
        signal: controller.signal,
        headers,
      });

      if (!response.ok) {
        // Surface the backend's own message (e.g. "AI not configured") rather
        // than a status code the dialog would have to translate itself.
        let body: unknown = null;
        try {
          body = await response.json();
        } catch {
          // not JSON; fall through with a generic message
        }
        if (isGateRequired(body)) dispatchGateRequired();
        const code = (body as { code?: unknown } | null)?.code;
        throw new ApiError(
          describeError(response.status, body),
          response.status,
          body,
          typeof code === 'string' ? code : undefined,
        );
      }
      if (!response.body) {
        throw new Error(`Event stream failed with status ${response.status}.`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (!closed) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Frames are separated by a blank line; keep any partial tail in `buffer`.
        let boundary = buffer.indexOf('\n\n');
        while (boundary !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          const event = parseFrame<T>(frame);
          if (event) handlers.onEvent(event);
          boundary = buffer.indexOf('\n\n');
        }
      }
      if (!closed) handlers.onClose?.();
    } catch (error) {
      if (closed) return;
      if (error instanceof DOMException && error.name === 'AbortError') return;
      handlers.onError?.(error instanceof Error ? error : new Error(String(error)));
    }
  };

  void run();

  return {
    close: () => {
      closed = true;
      controller.abort();
    },
  };
}

/**
 * Stream a run's events. `after` replays from a sequence number after a
 * reconnect.
 */
export function streamRun(
  runId: string,
  handlers: SseHandlers<RunEvent> & { after?: number },
): RunStream {
  return streamSse(
    `/api/runs/${runId}/events?after=${handlers.after ?? 0}`,
    {},
    handlers,
  );
}

/**
 * Stream a graph generation, including the trial run.
 *
 * The server sends a complete graph snapshot after every mutation, so the
 * dialog can render the draft growing without knowing anything about the
 * generator's tools. Closing the returned stream aborts the server-side
 * generation — nothing is applied, so the only loss is tokens.
 */
export function streamGenerateGraph(
  body: { prompt: string; graph?: WorkflowGraph; self_test?: boolean },
  handlers: SseHandlers<GenerationEvent>,
): RunStream {
  return streamSse(
    '/api/ai/generate-graph/stream',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    },
    handlers,
  );
}

/**
 * One copilot turn: understand, plan, edit, verify.
 *
 * Confirm mode ends the first stream with an `approval` event. The caller sends
 * that payload back as `approval` to execute the exact plan the user saw.
 */
export function streamCopilot(
  body: {
    message: string;
    graph: WorkflowGraph;
    mode: CopilotMode;
    selection?: { nodes: string[]; edges: string[] };
    sample_input?: Record<string, unknown> | null;
    expected_output?: string;
    last_output?: Record<string, unknown> | null;
    approval?: CopilotApproval | null;
    history?: { role: 'user' | 'assistant'; content: string }[];
    self_test?: boolean;
    thinking?: boolean;
    images?: string[];
  },
  handlers: SseHandlers<CopilotEvent>,
): RunStream {
  return streamSse(
    '/api/ai/copilot/stream',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ...body,
        lang: useLocale.getState().locale,
      }),
    },
    handlers,
  );
}

function parseFrame<T>(frame: string): T | null {
  const dataLines: string[] = [];
  for (const line of frame.split('\n')) {
    if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart());
  }
  if (dataLines.length === 0) return null;

  try {
    return JSON.parse(dataLines.join('\n')) as T;
  } catch {
    return null;
  }
}
