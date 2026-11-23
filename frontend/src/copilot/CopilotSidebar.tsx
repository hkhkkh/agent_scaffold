/**
 * The copilot conversation.
 *
 * This panel is deliberately only conversation plus the artifacts that belong
 * to the latest turn (the business brief, the plan waiting for approval, the
 * change report). Mode, write scope and the stage stack live above the canvas:
 * they are properties of the next action, not of the chat.
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  AlertTriangle,
  Bot,
  Brain,
  CheckCircle2,
  Globe,
  History,
  ImagePlus,
  ListChecks,
  Loader2,
  MessageSquarePlus,
  RotateCcw,
  Send,
  Sparkles,
  Square,
  XCircle,
} from 'lucide-react';

import { STAGE_TITLES } from '@/copilot/CopilotStageBar';
import {
  useCopilot,
  type CopilotTraceEntry,
} from '@/copilot/copilotStore';
import { useT } from '@/i18n';
import type { MessageKey } from '@/i18n/translations';
import { useWorkflow } from '@/store/workflowStore';
import type {
  CopilotBrief,
  CopilotPlan,
  CopilotResult,
  CopilotSource,
} from '@/types/workflow';

const CONFIDENCE: Record<string, MessageKey> = {
  high: 'copilot.confidenceHigh',
  medium: 'copilot.confidenceMedium',
  low: 'copilot.confidenceLow',
};

const ACTIONS: Record<string, MessageKey> = {
  update: 'copilot.actionUpdate',
  add: 'copilot.actionAdd',
  remove: 'copilot.actionRemove',
  connect: 'copilot.actionConnect',
  disconnect: 'copilot.actionDisconnect',
};

/**
 * Reasoning for one reply, rendered above that reply.
 *
 * Open while the turn is running so the user sees the thinking as it streams;
 * collapsed afterwards so a long transcript stays readable.
 */
function ThinkingBlock({ blocks, live }: { blocks: string[]; live: boolean }) {
  const t = useT();
  return (
    <details className="copilot-thinking" open={live}>
      <summary>
        <Brain size={11} />
        <span>{t('copilot.reasoning')}</span>
        <span className="copilot-thinking__count">{blocks.length}</span>
      </summary>
      <div className="copilot-thinking__body">
        {blocks.map((block, index) => (
          <div className="copilot-thinking__block" key={index}>
            <span className="copilot-thinking__round">
              {t('copilot.reasoningRound', { n: index + 1 })}
            </span>
            <p>{block}</p>
          </div>
        ))}
      </div>
    </details>
  );
}

const TRACE_KIND: Record<CopilotTraceEntry['kind'], MessageKey> = {
  tool: 'copilot.traceTool',
  validation: 'copilot.traceValidation',
  test: 'copilot.traceTest',
  correction: 'copilot.traceCorrection',
  review: 'copilot.traceReview',
};

/**
 * Every tool call, validation and trial run of the turn, in order.
 *
 * The stage strip shows only what is happening now. This is the audit trail:
 * "it edited, failed validation, corrected, edited again, tested" has to be
 * readable without opening devtools.
 */
function WorkTrace({ entries, live }: { entries: CopilotTraceEntry[]; live: boolean }) {
  const t = useT();
  if (entries.length === 0) return null;
  return (
    <details className="copilot-trace" open={live}>
      <summary>
        <ListChecks size={11} />
        <span>{t('copilot.trace')}</span>
        <span className="copilot-trace__count">{entries.length}</span>
      </summary>
      <ol className="copilot-trace__list">
        {entries.map((entry, index) => (
          <li className={`copilot-trace__item copilot-trace__item--${entry.status}`} key={entry.id}>
            <span className="copilot-trace__index">{index + 1}</span>
            <span className="copilot-trace__kind">{t(TRACE_KIND[entry.kind])}</span>
            <span className="copilot-trace__name">{entry.name}</span>
            {entry.status === 'running' ? (
              <Loader2 size={10} className="copilot-progress__spin" />
            ) : entry.status === 'failed' ? (
              <XCircle size={10} className="copilot-trace__fail" />
            ) : (
              <CheckCircle2 size={10} className="copilot-trace__ok" />
            )}
            {entry.detail ? (
              <pre className="copilot-trace__detail">{entry.detail}</pre>
            ) : null}
          </li>
        ))}
      </ol>
    </details>
  );
}

const MAX_IMAGES = 4;

function readAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error('read failed'));
    reader.readAsDataURL(file);
  });
}

/**
 * Keep an attached screenshot small enough for the API and the model.
 *
 * A raw phone screenshot is routinely 4–12 MB; base64 makes it larger still.
 * Anything above the comfortable threshold is scaled to 1600px and re-encoded
 * as JPEG. Text screenshots stay legible at that size, and the provider's
 * per-image token cap means a larger image buys nothing.
 */
async function prepareImage(file: File): Promise<string> {
  if (!file.type.startsWith('image/')) {
    throw new Error('not an image');
  }
  const raw = await readAsDataUrl(file);
  // Public mode caps the whole request at 1 MB; stay well under it so the JSON
  // envelope and the prompt do not turn a valid image into a 413.
  if (raw.length <= 700_000) return raw;

  const image = await new Promise<HTMLImageElement>((resolve, reject) => {
    const element = new Image();
    element.onload = () => resolve(element);
    element.onerror = () => reject(new Error('decode failed'));
    element.src = raw;
  });
  const scale = Math.min(1, 1600 / Math.max(image.width, image.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(image.width * scale));
  canvas.height = Math.max(1, Math.round(image.height * scale));
  const context = canvas.getContext('2d');
  if (!context) return raw;
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL('image/jpeg', 0.84);
}

export function CopilotSidebar() {
  const t = useT();
  const [text, setText] = useState('');
  const [showSample, setShowSample] = useState(false);
  const [sampleText, setSampleText] = useState('');
  const [expected, setExpected] = useState('');
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [images, setImages] = useState<string[]>([]);
  const [imageError, setImageError] = useState<string | null>(null);
  const imageInputRef = useRef<HTMLInputElement>(null);

  const messages = useCopilot((state) => state.messages);
  const lastAssistantId = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]?.role === 'assistant') return messages[index]?.id ?? null;
    }
    return null;
  }, [messages]);
  const hasAttachedThinking = messages.some((entry) => entry.thinking?.length);
  const running = useCopilot((state) => state.running);
  const stages = useCopilot((state) => state.stages);
  const trace = useCopilot((state) => state.trace);
  const activeStage = useMemo(
    () =>
      [...stages]
        .reverse()
        .find((stage) => stage.status === 'running' || stage.status === 'progress') ??
      null,
    [stages],
  );
  const activeStageTitle = activeStage ? STAGE_TITLES[activeStage.id] : undefined;
  const waiting = useCopilot((state) => state.waiting);
  const brief = useCopilot((state) => state.brief);
  const plan = useCopilot((state) => state.plan);
  const research = useCopilot((state) => state.research);
  const result = useCopilot((state) => state.result);
  const thinking = useCopilot((state) => state.thinking);
  const streamingId = useCopilot((state) => state.streamingId);
  const usage = useCopilot((state) => state.usage);
  const conversations = useCopilot((state) => state.conversations);
  const historyOpen = useCopilot((state) => state.historyOpen);
  const activeConversationId = useCopilot((state) => state.activeConversationId);
  const send = useCopilot((state) => state.send);
  const stop = useCopilot((state) => state.stop);
  const reset = useCopilot((state) => state.reset);
  const toggleHistory = useCopilot((state) => state.toggleHistory);
  const loadConversation = useCopilot((state) => state.loadConversation);
  const deleteConversation = useCopilot((state) => state.deleteConversation);
  const aiStatus = useWorkflow((state) => state.aiStatus);
  const graphId = useWorkflow((state) => state.graphId);
  // A conversation belongs to one workflow; never show another workflow's
  // transcript, even though all of them share one browser store.
  const visibleConversations = conversations.filter(
    (entry) => entry.graphId === graphId,
  );

  const listRef = useRef<HTMLDivElement>(null);
  const aiReady = !aiStatus || (aiStatus.enabled && aiStatus.configured);

  useEffect(() => {
    const element = listRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
  }, [messages, brief, plan, result, running, thinking, trace]);

  const submit = () => {
    const message = text.trim();
    if ((!message && images.length === 0) || running || !aiReady) return;

    let sampleInput: Record<string, unknown> | null = null;
    if (showSample && sampleText.trim()) {
      try {
        const parsed: unknown = JSON.parse(sampleText);
        if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
          setJsonError(t('copilot.sampleInput'));
          return;
        }
        sampleInput = parsed as Record<string, unknown>;
      } catch {
        setJsonError(t('copilot.sampleInput'));
        return;
      }
    }

    setJsonError(null);
    send(message, {
      sampleInput,
      expectedOutput: showSample ? expected : '',
      images,
    });
    setText('');
    setImages([]);
    setImageError(null);
  };

  const addImages = async (files: FileList | null) => {
    if (!files?.length) return;
    setImageError(null);
    const available = MAX_IMAGES - images.length;
    const chosen = Array.from(files).slice(0, Math.max(0, available));
    if (files.length > available) {
      setImageError(t('copilot.imageLimit', { n: MAX_IMAGES }));
      return;
    }
    try {
      const prepared = await Promise.all(chosen.map(prepareImage));
      setImages((current) => [...current, ...prepared].slice(0, MAX_IMAGES));
    } catch {
      setImageError(t('copilot.imageInvalid'));
    }
  };

  return (
    <div className="copilot">
      <div className="copilot__header">
        <div className="copilot__title">
          <Bot size={13} />
          <span>{t('copilot.title')}</span>
          {usage && usage.total_tokens > 0 ? (
            <span className="copilot-usage" title={t('copilot.tokensHint')}>
              {t('copilot.tokens', { n: usage.total_tokens.toLocaleString() })}
            </span>
          ) : null}
        </div>
        <div className="copilot__header-actions">
          <button
            type="button"
            className={`btn btn--ghost btn--icon btn--sm${historyOpen ? ' btn--active' : ''}`}
            title={t('copilot.history')}
            onClick={toggleHistory}
          >
            <History size={12} />
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--icon btn--sm"
            title={t('copilot.newChat')}
            onClick={reset}
          >
            <MessageSquarePlus size={12} />
          </button>
        </div>
      </div>

      {historyOpen ? (
        <div className="copilot-history">
          {visibleConversations.length === 0 ? (
            <p className="copilot-history__empty">{t('copilot.historyEmpty')}</p>
          ) : (
            visibleConversations.map((entry) => (
              <div
                key={entry.id}
                className={`copilot-history__item${
                  entry.id === activeConversationId ? ' copilot-history__item--active' : ''
                }`}
              >
                <button
                  type="button"
                  className="copilot-history__open"
                  onClick={() => loadConversation(entry.id)}
                  title={entry.title}
                >
                  <span className="copilot-history__title">{entry.title}</span>
                  <span className="copilot-history__when">
                    {formatWhen(entry.at)}
                  </span>
                </button>
                <button
                  type="button"
                  className="copilot-history__delete"
                  title={t('copilot.historyDelete')}
                  onClick={() => deleteConversation(entry.id)}
                >
                  <XCircle size={11} />
                </button>
              </div>
            ))
          )}
        </div>
      ) : null}

      <div className="copilot__messages" ref={listRef}>
        {messages.length === 0 ? (
          <div className="copilot__empty">{t('copilot.empty')}</div>
        ) : (
          messages.map((entry) => {
            // Old conversations kept one reasoning array per turn. Attach it to
            // the last assistant message here as well as on load, so a live
            // window that has not been reloaded does not keep the old order.
            const blocks = entry.thinking?.length
              ? entry.thinking
              : !hasAttachedThinking &&
                  entry.id === lastAssistantId &&
                  thinking.length > 0
                ? thinking
                : [];
            return (
            <div
              key={entry.id}
              className={[
                'copilot-msg',
                `copilot-msg--${entry.role}`,
                entry.kind ? `copilot-msg--${entry.kind}` : '',
              ]
                .filter(Boolean)
                .join(' ')}
            >
              {blocks.length > 0 ? (
                <ThinkingBlock
                  blocks={blocks}
                  live={running && entry.id === streamingId}
                />
              ) : null}
              {entry.role === 'assistant' &&
              entry.kind !== 'error' &&
              entry.kind !== 'status' &&
              entry.text ? (
                <div className="copilot-msg__markdown">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{entry.text}</ReactMarkdown>
                </div>
              ) : (
                entry.text || null
              )}
            </div>
            );
          })
        )}

        {running && activeStage && !(activeStage.id === 'insight' && streamingId) ? (
          <div className="copilot-progress" role="status">
            <Loader2 size={12} className="copilot-progress__spin" />
            <span>{activeStageTitle ? t(activeStageTitle) : activeStage.id}</span>
            {activeStage.detail ? (
              <span className="copilot-progress__detail">{activeStage.detail}</span>
            ) : null}
          </div>
        ) : null}

        <WorkTrace entries={trace} live={running} />

        {brief && (!brief.request_kind || brief.request_kind === 'edit') ? (
          <BriefCard brief={brief} research={research} />
        ) : null}
        {plan && (waiting || !result) ? <PlanCard plan={plan} /> : null}
        {result &&
        !result.needs_approval &&
        (result.applied ||
          result.test ||
          result.output_review ||
          result.changes.added_nodes.length +
            result.changes.removed_nodes.length +
            result.changes.updated_nodes.length +
            result.changes.added_edges.length +
            result.changes.removed_edges.length +
            result.changes.updated_edges.length >
            0) ? (
          <ReportCard result={result} />
        ) : null}
      </div>

      <div className="copilot__composer">
        {!aiReady ? (
          <div className="copilot__warning">
            <AlertTriangle size={11} />
            {aiStatus?.reason ?? t('copilot.errorPrefix', { message: 'AI' })}
          </div>
        ) : null}

        {showSample ? (
          <div className="copilot-sample">
            <p className="copilot-sample__hint">{t('copilot.sampleHint')}</p>
            <label className="copilot-sample__field">
              <span>{t('copilot.sampleInput')}</span>
              <textarea
                value={sampleText}
                onChange={(event) => {
                  setSampleText(event.target.value);
                  setJsonError(null);
                }}
                placeholder='{"query": "..."}'
                rows={2}
                spellCheck={false}
              />
            </label>
            <label className="copilot-sample__field">
              <span>{t('copilot.expectedOutput')}</span>
              <textarea
                value={expected}
                onChange={(event) => setExpected(event.target.value)}
                placeholder="..."
                rows={2}
              />
            </label>
            {jsonError ? <p className="copilot-sample__error">{jsonError}</p> : null}
          </div>
        ) : null}

        {images.length > 0 ? (
          <div className="copilot-attachments">
            {images.map((src, index) => (
              <div className="copilot-attachment" key={`${src.slice(0, 32)}-${index}`}>
                <img src={src} alt="" />
                <button
                  type="button"
                  className="copilot-attachment__remove"
                  title={t('copilot.imageRemove')}
                  onClick={() =>
                    setImages((current) => current.filter((_, at) => at !== index))
                  }
                >
                  <XCircle size={11} />
                </button>
              </div>
            ))}
          </div>
        ) : null}
        {imageError ? <p className="copilot-sample__error">{imageError}</p> : null}

        <textarea
          className="copilot__input"
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={(event) => {
            // Enter sends; Shift+Enter is a newline. Skip while an IME is
            // composing, or choosing a Chinese candidate would submit the turn.
            if (
              event.key === 'Enter' &&
              !event.shiftKey &&
              !event.nativeEvent.isComposing
            ) {
              event.preventDefault();
              submit();
            }
          }}
          title={t('copilot.enterHint')}
          placeholder={t('copilot.placeholder')}
          rows={3}
          disabled={running || !aiReady}
        />

        <div className="copilot__actions">
          <button
            type="button"
            className={`btn btn--ghost btn--sm${showSample ? ' btn--active' : ''}`}
            onClick={() => setShowSample(!showSample)}
            title={t('copilot.sampleHint')}
          >
            <ListChecks size={11} /> {t('copilot.sampleInput')}
          </button>
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            title={t('copilot.imageHint')}
            disabled={running || !aiReady || images.length >= MAX_IMAGES}
            onClick={() => imageInputRef.current?.click()}
          >
            <ImagePlus size={11} /> {t('copilot.image')}
          </button>
          <input
            ref={imageInputRef}
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            multiple
            hidden
            onChange={(event) => {
              void addImages(event.target.files);
              event.target.value = '';
            }}
          />
          <div className="spacer" />
          {running ? (
            <button type="button" className="btn btn--sm" onClick={stop}>
              <Square size={10} /> {t('copilot.stop')}
            </button>
          ) : (
            <button
              type="button"
              className="btn btn--primary btn--sm"
              disabled={(!text.trim() && images.length === 0) || !aiReady}
              onClick={submit}
            >
              <Send size={11} /> {t('copilot.send')}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Artifacts
// ---------------------------------------------------------------------------

function BriefCard({
  brief,
  research,
}: {
  brief: CopilotBrief;
  research: ReturnType<typeof useCopilot.getState>['research'];
}) {
  const t = useT();
  const sources = useMemo(
    () => uniqueSources(brief.sources ?? [], research?.sources ?? []),
    [brief.sources, research?.sources],
  );

  return (
    <section className="copilot-card copilot-card--brief">
      <header className="copilot-card__header">
        <Sparkles size={12} />
        <span>{t('copilot.goal')}</span>
        <span className={`copilot-chip copilot-chip--${brief.confidence}`}>
          {CONFIDENCE[brief.confidence] ? t(CONFIDENCE[brief.confidence]!) : brief.confidence}
        </span>
      </header>

      <p className="copilot-card__lead">{brief.goal}</p>
      {brief.direction ? (
        <div className="copilot-field">
          <span className="copilot-field__label">{t('copilot.direction')}</span>
          <span>{brief.direction}</span>
        </div>
      ) : null}

      {brief.explicit_requirements.length > 0 ? (
        <div className="copilot-field">
          <span className="copilot-field__label">{t('copilot.explicit')}</span>
          <ul className="copilot-list">
            {brief.explicit_requirements.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {brief.success_criteria.length > 0 ? (
        <ul className="copilot-list">
          {brief.success_criteria.map((item, index) => (
            <li key={index}>{item}</li>
          ))}
        </ul>
      ) : null}

      {brief.assumptions.length > 0 ? (
        <div className="copilot-field">
          <span className="copilot-field__label">{t('copilot.assumptions')}</span>
          <ul className="copilot-assumptions">
            {brief.assumptions.map((assumption, index) => (
              <li key={index}>
                <span className={`copilot-kind copilot-kind--${assumption.kind}`}>
                  {assumption.kind === 'assumed' ? t('copilot.assumed') : t('copilot.inferred')}
                </span>
                <span>{assumption.text}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {brief.open_questions.length > 0 ? (
        <div className="copilot-field">
          <span className="copilot-field__label">{t('copilot.openQuestions')}</span>
          <ul className="copilot-list copilot-list--quiet">
            {brief.open_questions.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <div className="copilot-research">
        <Globe size={11} />
        {research?.status === 'ok' ? (
          <span>{t('copilot.researchOk', { count: sources.length })}</span>
        ) : research?.status === 'skipped' || !research ? (
          <span>{t('copilot.researchSkipped')}</span>
        ) : (
          <span title={research.error ?? undefined}>{t('copilot.researchFailed')}</span>
        )}
      </div>

      {sources.length > 0 ? (
        <div className="copilot-sources">
          {sources.slice(0, 5).map((source) => (
            <a
              key={source.url ?? source.title}
              href={source.url}
              target="_blank"
              rel="noreferrer noopener"
              className="copilot-source"
              title={source.snippet}
            >
              {source.title || source.url}
            </a>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function PlanCard({ plan }: { plan: CopilotPlan }) {
  const t = useT();
  const approve = useCopilot((state) => state.approve);
  const discardPlan = useCopilot((state) => state.discardPlan);
  const running = useCopilot((state) => state.running);
  const waiting = useCopilot((state) => state.waiting);

  return (
    <section className="copilot-card copilot-card--plan">
      <header className="copilot-card__header">
        <ListChecks size={12} />
        <span>{t('copilot.plan')}</span>
      </header>

      {plan.diagnosis ? <p className="copilot-card__lead">{plan.diagnosis}</p> : null}

      {plan.changes.length > 0 ? (
        <ul className="copilot-changes">
          {plan.changes.map((change, index) => (
            <li key={index}>
              <span className={`copilot-action copilot-action--${change.action}`}>
                {ACTIONS[change.action] ? t(ACTIONS[change.action]!) : change.action}
              </span>
              <span className="copilot-change__target">{change.target}</span>
              {change.why ? <span className="copilot-change__why">{change.why}</span> : null}
            </li>
          ))}
        </ul>
      ) : null}

      {plan.steps.length > 0 ? (
        <ol className="copilot-list copilot-list--ordered">
          {plan.steps.map((step, index) => (
            <li key={index}>{step}</li>
          ))}
        </ol>
      ) : null}

      {plan.scope_expansion_needed ? (
        <p className="copilot-warning">
          <AlertTriangle size={11} /> {t('copilot.scopeExpansion')}
        </p>
      ) : null}

      {plan.risks.length > 0 ? (
        <div className="copilot-field">
          <span className="copilot-field__label">{t('copilot.risks')}</span>
          <ul className="copilot-list copilot-list--quiet">
            {plan.risks.map((risk, index) => (
              <li key={index}>{risk}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {waiting ? (
        <div className="copilot-card__actions">
          <button
            type="button"
            className="btn btn--primary btn--sm"
            onClick={approve}
            disabled={running}
          >
            {t('copilot.approve')}
          </button>
          <button type="button" className="btn btn--ghost btn--sm" onClick={discardPlan}>
            {t('copilot.discard')}
          </button>
        </div>
      ) : null}
    </section>
  );
}

function ReportCard({ result }: { result: CopilotResult }) {
  const t = useT();
  const revert = useCopilot((state) => state.revert);
  const preEdit = useCopilot((state) => state.preEdit);

  const nodes =
    result.changes.added_nodes.length +
    result.changes.removed_nodes.length +
    result.changes.updated_nodes.length;
  const edges =
    result.changes.added_edges.length +
    result.changes.removed_edges.length +
    result.changes.updated_edges.length;

  return (
    <section className="copilot-card copilot-card--report">
      <header className="copilot-card__header">
        <CheckCircle2 size={12} />
        <span>{nodes > 0 || edges > 0 ? t('copilot.report') : t('copilot.answer')}</span>
      </header>

      {result.summary ? <p className="copilot-card__lead">{result.summary}</p> : null}
      {nodes > 0 || edges > 0 ? (
        <p className="copilot-report__counts">
          {t('copilot.changed', { nodes, edges })}
        </p>
      ) : null}

      {result.test ? (
        <p
          className={`copilot-report__test${result.test.ok ? ' copilot-report__test--ok' : ' copilot-report__test--bad'}`}
        >
          {result.test.ok ? <CheckCircle2 size={11} /> : <XCircle size={11} />}
          {result.test.ok ? t('copilot.testPassed') : t('copilot.testFailed')}
          {result.test.ok && result.test.duration_ms != null
            ? ` · ${Math.round(result.test.duration_ms)}ms`
            : result.test.error
              ? ` · ${result.test.error}`
              : ''}
        </p>
      ) : null}

      {result.output_review ? (
        <p
          className={`copilot-report__test${result.output_review.matches ? ' copilot-report__test--ok' : ' copilot-report__test--bad'}`}
        >
          {result.output_review.matches ? <CheckCircle2 size={11} /> : <XCircle size={11} />}
          {result.output_review.matches
            ? t('copilot.outputMatched')
            : t('copilot.outputMismatch')}
          {result.output_review.reason ? ` · ${result.output_review.reason}` : ''}
        </p>
      ) : null}

      {result.applied ? (
        <p className="copilot-report__versioned">{t('copilot.versioned')}</p>
      ) : null}

      {result.applied ? (
        <div className="copilot-card__actions">
          <button
            type="button"
            className="btn btn--ghost btn--sm"
            onClick={() => window.dispatchEvent(new CustomEvent('scaffold:open-versions'))}
          >
            <History size={11} /> {t('copilot.versions')}
          </button>
          {preEdit ? (
            <button type="button" className="btn btn--ghost btn--sm" onClick={revert}>
              <RotateCcw size={11} /> {t('copilot.revert')}
            </button>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function formatWhen(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  return date.toDateString() === today.toDateString()
    ? date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    : date.toLocaleDateString();
}

function uniqueSources(...groups: CopilotSource[][]): CopilotSource[] {
  const seen = new Set<string>();
  const out: CopilotSource[] = [];
  for (const group of groups) {
    for (const source of group) {
      const key = source.url || source.title || '';
      if (key && !seen.has(key)) {
        seen.add(key);
        out.push(source);
      }
    }
  }
  return out;
}
