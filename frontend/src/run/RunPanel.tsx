/**
 * Run panel — the timeline, the inputs, and the result.
 *
 * The design goal is that a run is *legible without scrolling to the bottom*:
 * each step reports its own status and duration in the timeline, and its inputs
 * and outputs are one click away rather than all expanded at once. A workflow
 * with a dozen nodes would otherwise be an unreadable wall of text.
 */

import { useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Ban,
  Bug,
  Check,
  ChevronRight,
  Clock,
  Copy,
  Loader2,
  Play,
  RotateCcw,
  Sparkles,
  Wrench,
  XCircle,
  type LucideIcon,
} from 'lucide-react';

import { useWorkflow } from '@/store/workflowStore';
import { api } from '@/api/client';
import { watchRun } from '@/run/runController';
import { finalOutput } from '@/run/finalOutput';
import type {
  FinalOutput,
  RunEvent,
  RunStep,
  RunSummary,
  RunUsage,
  TrialRunEvent,
  TrialRunResult,
} from '@/types/workflow';
import { useT } from '@/i18n';
import { JsonViewer } from '@/ui/JsonViewer';
import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';

/**
 * Turn the trial timeline into the same row shape a platform run uses.
 *
 * A loop re-runs the same node id, so rows are keyed by the live object and a
 * finish updates the latest run of that id; the event sequence is the order.
 */
function trialSteps(trace: TrialRunEvent[] | undefined): RunStep[] {
  const steps: RunStep[] = [];
  const current: Record<string, RunStep> = {};
  for (const event of trace ?? []) {
    const nodeId = event.node_id ?? '';
    if (event.kind === 'node_started') {
      const step: RunStep = {
        node_id: nodeId,
        node_label: event.node || nodeId,
        index: steps.length + 1,
        status: 'running',
        inputs: event.inputs ?? null,
        outputs: null,
        error: null,
        duration_ms: null,
        logs: [],
      };
      steps.push(step);
      current[nodeId] = step;
      continue;
    }
    if (event.kind === 'node_finished' || event.kind === 'node_failed') {
      const step =
        current[nodeId] ??
        (() => {
          const created: RunStep = {
            node_id: nodeId,
            node_label: event.node || nodeId,
            index: steps.length + 1,
            status: 'running',
            inputs: null,
            outputs: null,
            error: null,
            duration_ms: null,
            logs: [],
          };
          steps.push(created);
          current[nodeId] = created;
          return created;
        })();
      step.status = event.kind === 'node_finished' ? 'succeeded' : 'failed';
      step.outputs = event.outputs ?? null;
      step.error = event.error ?? null;
      step.duration_ms = event.duration_ms ?? null;
      continue;
    }
    if (event.kind === 'log') {
      const step = current[nodeId] ?? steps[steps.length - 1];
      if (step) step.logs = [...(step.logs ?? []), event.message ?? ''];
    }
  }
  return steps;
}

/**
 * The copilot's trial run, in the Run tab.
 *
 * It is a draft test, not a RunManager run: it has no persisted row, so it
 * cannot appear in `RunHistoryList`. Saying that on the card is the honest
 * alternative to letting it look like a run that will survive a reload.
 */
function SelfTestRun({
  result,
  onDismiss,
}: {
  result: TrialRunResult;
  onDismiss: () => void;
}) {
  const t = useT();
  const steps = useMemo(() => trialSteps(result.trace), [result.trace]);
  const status =
    result.status === 'passed'
      ? 'succeeded'
      : result.status === 'paused'
        ? 'interrupted'
        : result.status === 'skipped'
          ? 'cancelled'
          : 'failed';

  return (
    <section className="run__selftest">
      <div className="run__selftest-head">
        <span className="section-label">{t('run.selfTest')}</span>
        <StatusBadge status={status} />
        {typeof result.duration_ms === 'number' ? (
          <span className="run__elapsed">{formatDuration(result.duration_ms)}</span>
        ) : null}
        <span className="dim run__selftest-hint">{t('run.selfTestHint')}</span>
        <div className="spacer" />
        <button
          type="button"
          className="btn btn--ghost btn--icon btn--sm"
          title={t('run.selfTestDismiss')}
          onClick={onDismiss}
        >
          <XCircle size={11} />
        </button>
      </div>
      {result.error ? (
        <div className="callout callout--error">
          <AlertCircle size={12} className="callout__icon" />
          <span>{result.error}</span>
        </div>
      ) : (
        <p className="field__hint">{result.summary}</p>
      )}
      {steps.length > 0 ? (
        <ol className="run__steps">
          {steps.map((step) => (
            <StepRow
              key={`${step.node_id}-${step.index}`}
              step={step}
              streamText={undefined}
              activity={undefined}
            />
          ))}
        </ol>
      ) : null}
    </section>
  );
}

export function RunPanel({ onRun }: { onRun: () => void }) {
  const t = useT();
  const runId = useWorkflow((state) => state.runId);
  const runStatus = useWorkflow((state) => state.runStatus);
  const runEvents = useWorkflow((state) => state.runEvents);
  const runOutputs = useWorkflow((state) => state.runOutputs);
  const runError = useWorkflow((state) => state.runError);
  const runUsage = useWorkflow((state) => state.runUsage);
  const runInterrupt = useWorkflow((state) => state.runInterrupt);
  const stepByNode = useWorkflow((state) => state.stepByNode);
  const selfTest = useWorkflow((state) => state.selfTest);
  const setSelfTest = useWorkflow((state) => state.setSelfTest);
  const cancelRun = useWorkflow((state) => state.cancelRun);
  const clearRun = useWorkflow((state) => state.clearRun);

  const steps = useMemo(
    () => Object.values(stepByNode).sort((a, b) => a.index - b.index),
    [stepByNode],
  );

  /**
   * The run's answer, when it produced exactly one.
   *
   * `finalOutput` refuses to guess — it returns `null` when the output holds
   * several candidates or only a partial dump. The panel keeps that refusal
   * rather than reaching for a field of its own: showing the wrong variable is
   * worse than showing nothing, because it reads as something the agent said.
   */
  const answer = useMemo(() => finalOutput(runOutputs), [runOutputs]);

  /**
   * What each node did, in the order it did it.
   *
   * Derived from the event stream rather than accumulated in the store: every
   * line here is already on the wire — a node's own logs arrive as `log` events
   * and the agent node reports its tool calls on a chunk event — and the store
   * keeps every event, so a second copy would be the same history twice.
   */
  const activityByNode = useMemo(() => buildActivity(runEvents), [runEvents]);
  /**
   * Accumulated streaming text per node.
   *
   * Computed once here rather than per step: every chunk carries the full text
   * so far, so the generator only needs the last one, but recomputing that for
   * each row on each event would be quadratic in a streaming run.
   */
  const streamByNode = useMemo(() => {
    const map: Record<string, string> = {};
    for (const event of runEvents) {
      if (event.type !== 'node_output_chunk' || !event.node_id) continue;
      const text = event.data.text;
      if (typeof text === 'string') map[event.node_id] = text;
    }
    return map;
  }, [runEvents]);

  const running = runStatus === 'running';

  // Elapsed time is derived from the first event's timestamp rather than a
  // local clock, so it stays correct if the panel re-mounts mid-run.
  const startedAt = useMemo(() => {
    const first = runEvents.find((event) => event.type === 'run_started') ?? runEvents[0];
    return first ? Date.parse(first.timestamp) : null;
  }, [runEvents]);

  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [running]);

  // Periodic poll to safeguard against lost SSE frames or closed streams. The
  // implementation lives in the controller so both views share one watchdog
  // rather than growing a second copy of it.
  useEffect(() => {
    if (!running || !runId) return;
    return watchRun(runId);
  }, [running, runId]);

  if (!runId && !running && !selfTest && !runError) {
    return (
      <div className="run run--empty">
        <p className="empty">
          {t('run.empty')}
          <br />
          <span className="dim">{t('run.emptyHint')}</span>
        </p>
        <button type="button" className="btn btn--primary" onClick={onRun}>
          <Play size={12} /> {t('run.start')}
        </button>
        <RunHistoryList />
      </div>
    );
  }

  // A trial exists but no platform run does. Show it without pretending it is
  // one of the persisted runs in history.
  if (!runId && !running && selfTest) {
    return (
      <div className="run">
        <header className="run__bar">
          <span className="badge">{t('run.idle')}</span>
          <div className="spacer" />
          <button type="button" className="btn btn--primary btn--sm" onClick={onRun}>
            <Play size={11} /> {t('app.run')}
          </button>
        </header>
        <div className="run__body scroll">
          <SelfTestRun result={selfTest} onDismiss={() => setSelfTest(null)} />
          <RunHistoryList />
        </div>
      </div>
    );
  }

  const elapsedMs = startedAt !== null ? Math.max(0, now - startedAt) : null;

  return (
    <div className="run">
      <header className="run__bar">
        <StatusBadge status={runStatus} />
        {running && elapsedMs !== null ? (
          <span className="run__elapsed">
            {t('run.elapsed', { seconds: (elapsedMs / 1000).toFixed(1) })}
          </span>
        ) : null}
        {!running && steps.length > 0 ? (
          <span className="run__elapsed">
            {t(steps.length === 1 ? 'run.stepCount.one' : 'run.stepCount.many', {
              count: steps.length,
            })}
          </span>
        ) : null}

        <div className="spacer" />

        {running ? (
          <button type="button" className="btn btn--danger btn--sm" onClick={() => void cancelRun()}>
            <Ban size={11} /> {t('run.cancel')}
          </button>
        ) : (
          <>
            <button
              type="button"
              className="btn btn--ghost btn--icon btn--sm"
              title={t('run.clear')}
              onClick={clearRun}
            >
              <RotateCcw size={11} />
            </button>
            <button type="button" className="btn btn--primary btn--sm" onClick={onRun}>
              <Play size={11} /> {t('app.run')}
            </button>
          </>
        )}
      </header>

      <div className="run__body scroll">
        {runError && runStatus !== 'cancelled' ? (
          <div className="callout callout--error run__error">
            <AlertCircle size={13} className="callout__icon" />
            <span>{runError}</span>
          </div>
        ) : null}

        {/* Stopping a run is a decision, not a failure: the status badge says
            "cancelled" and this note confirms it without red alarm styling. */}
        {runStatus === 'cancelled' ? (
          <p className="field__hint">{t('run.cancelledNote')}</p>
        ) : null}

        {/* A paused run is not an error and not a success — it is a question
            aimed at the person watching, so it says so in those words. */}
        {runInterrupt ? (
          <div className="callout callout--warning run__interrupt">
            <AlertCircle size={13} className="callout__icon" />
            <span>{t('run.interruptWaiting')}</span>
          </div>
        ) : null}

        {selfTest ? (
          <SelfTestRun result={selfTest} onDismiss={() => setSelfTest(null)} />
        ) : null}

        {/* While it runs, the live spend belongs at the top where the eye is;
            after it ends the same line stays under the trace. */}
        {running ? <UsageLine usage={runUsage} /> : null}

        {steps.length === 0 && running ? (
          <p className="field__hint">{t('run.starting')}</p>
        ) : null}

        {/* The answer is read first and the machinery second. A finished run is
            opened for its result; the steps are there for when it is wrong. */}
        {!running && answer ? <AnswerCard answer={answer} /> : null}

        {!running && !answer && Object.keys(runOutputs).length > 0 ? (
          <div className="callout callout--muted run__answer-missing">
            <span>{t('run.answerEmpty')}</span>
          </div>
        ) : null}

        {steps.length > 0 ? (
          <div className="section-label run__section-label">{t('run.trace')}</div>
        ) : null}

        <ol className="run__steps">
          {steps.map((step) => (
            <StepRow
              key={step.node_id}
              step={step}
              streamText={streamByNode[step.node_id]}
              activity={activityByNode[step.node_id]}
            />
          ))}
        </ol>

        <UsageLine usage={runUsage} />

        {!running && steps.length > 0 && Object.keys(runOutputs).length === 0 && !runError ? (
          <p className="field__hint">{t('run.noOutputs')}</p>
        ) : null}

        {!running && Object.keys(runOutputs).length > 0 ? (
          <DebugDetails outputs={runOutputs} />
        ) : null}
      </div>
    </div>
  );
}

/**
 * Past runs, fetched on demand.
   *
   * Read-only and local to this component: history is not something the rest of
   * the app acts on, so putting it in the store would add state that nothing else
   * reads. The fetch is on click rather than on mount because the endpoint hits
   * the database, and the empty state is shown often enough that paying for it
   * every time would be wasteful.
   *
 * Which endpoint: `GET /runs/history` (the database), **not** `GET /runs` (what
 * is still in memory). The latter is a shrinking window — a finished run is
 * evicted after a retention period — so it is the wrong thing to label
 * "history".
 */
function RunHistoryList() {
  const t = useT();
  // `graphId`, not `workflowId` — that is the name the store uses for the
  // current document's id (`workflowStore.ts`).
  const workflowId = useWorkflow((state) => state.graphId);
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = async () => {
    setError(null);
    try {
      const payload = await api.getRunHistory(workflowId ?? undefined);
      setRuns(payload.runs);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  const selectHistory = async (run: RunSummary) => {
    try {
      const { steps } = await api.getRunSteps(run.id);
      useWorkflow.getState().finishRun(run);
      if (steps) {
        const stepByNode: Record<string, RunStep> = {};
        steps.forEach((s) => {
          stepByNode[s.node_id] = s;
        });
        useWorkflow.setState({ stepByNode, runId: run.id });
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  };

  if (runs === null) {
    return (
      <div className="run__history">
        <button type="button" className="btn btn--ghost btn--sm" onClick={() => void load()}>
          {t('run.historyLoad')}
        </button>
        {error ? <p className="field__hint">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className="run__history">
      <div className="section-label">{t('run.history')}</div>
      {runs.length === 0 ? (
        <p className="field__hint">{t('run.historyEmpty')}</p>
      ) : (
        <ul className="run__history-list">
          {runs.map((run) => (
            <li
              key={run.id}
              className="mono"
              style={{ cursor: 'pointer' }}
              title="点击查看此运行的详细步骤与结果"
              onClick={() => void selectHistory(run)}
            >
              <StatusBadge status={run.status} />
              <span className="dim">{run.id}</span>
              <span className="dim">{run.created_at ?? ''}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="field__hint">{t('run.historyHint')}</p>
    </div>
  );
}


/**
 * The run's answer, given the most prominent place in the panel.
 *
 * This is the product's whole point: everything else in here is machinery for
 * producing these few lines, and for a long time the machinery was displayed
 * and the answer was buried ten levels into it.
 *
 * Only the answer's own text is styled as prose. A non-string value is shown as
 * JSON — stringifying an object into a paragraph would read as though the agent
 * had written that sentence.
 */
function AnswerCard({ answer }: { answer: FinalOutput }) {
  const t = useT();
  const [copied, setCopied] = useState(false);

  const text =
    typeof answer.value === 'string' ? answer.value : JSON.stringify(answer.value, null, 2);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // The clipboard can be refused (insecure origin, permission). The text is
      // on screen and selectable, so a failed copy needs no error surface.
    }
  };

  return (
    <section className="answer">
      <header className="answer__head">
        <Sparkles size={13} className="answer__icon" />
        <span className="answer__title">{t('run.answer')}</span>
        <span className="answer__source mono">{t('run.answerField', { key: answer.key })}</span>
        <div className="spacer" />
        <button type="button" className="btn btn--ghost btn--sm" onClick={() => void copy()}>
          {copied ? <Check size={11} /> : <Copy size={11} />}
          {copied ? t('run.answerCopied') : t('run.answerCopy')}
        </button>
      </header>
      {/*
        A text answer is rendered as Markdown rather than pre-wrapped text.
        Agents overwhelmingly answer in Markdown (headings, lists, bold, tables),
        and showing the raw asterisks is the difference between "a document" and
        "a debug field". A non-string answer stays JSON: rendering an object as
        prose would read as though the agent had written that sentence.
      */}
      <div
        className={`answer__body${
          typeof answer.value === 'string' ? ' answer__prose markdown-body' : ' mono'
        }`}
      >
        {typeof answer.value === 'string' ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeHighlight]}>
            {text}
          </ReactMarkdown>
        ) : (
          text
        )}
      </div>
    </section>
  );
}

/**
 * Everything a developer wants and a customer does not: the raw output object.
 *
 * Collapsed by default, and deliberately placed below the trace. It is not
 * hidden — a wrong answer has to be diagnosable — but it is no longer the first
 * thing the panel shows.
 */
function DebugDetails({ outputs }: { outputs: Record<string, unknown> }) {
  const t = useT();
  const [open, setOpen] = useState(false);

  return (
    <div className="run__debug">
      <button
        type="button"
        className="run__debug-toggle"
        aria-expanded={open}
        onClick={() => setOpen((previous) => !previous)}
      >
        <ChevronRight size={11} className={`run-step__chevron${open ? ' is-open' : ''}`} />
        <Bug size={11} />
        <span>{t('run.debug')}</span>
        <span className="dim">{t('run.debugHint')}</span>
      </button>
      {open ? (
        <div className="run__debug-body">
          <JsonViewer value={outputs} defaultDepth={2} maxHeight={320} />
        </div>
      ) : null}
    </div>
  );
}

function UsageLine({ usage }: { usage: RunUsage | null }) {
  const t = useT();
  if (!usage) return null;

  // "not reported" rather than 0 when the provider never said: unreported is not
  // the same as none, and showing 0 for an unknown figure is how a cost estimate
  // becomes a lie.
  const tokens = usage.tokens_known
    ? t('run.usage.tokens').replace('{n}', String(usage.tokens))
    : t('run.usage.tokensUnknown');

  const parts = [
    usage.max_tokens ? `${tokens} / ${usage.max_tokens}` : tokens,
    t('run.usage.steps').replace('{n}', String(usage.steps)),
    `${usage.elapsed_seconds.toFixed(1)}s`,
  ];

  return <p className="field__hint run__usage">{parts.join(' · ')}</p>;
}

type KnownRunState =
  | 'pending'
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'interrupted'
  | 'cancelled'
  | 'stopped';

/**
 * Run status rendered as a glyph and a tint, with words a customer would use.
 *
 * The label used to be the raw value from the API, so a finished Chinese screen
 * read `succeeded`, `interrupted`, `stopped` — English identifiers in the middle
 * of the interface, and that last one is a status this client's own union does
 * not even list. Colour alone would not do either: colour does not survive a
 * screenshot into a bug report.
 */
const RUN_STATES: Record<KnownRunState, { tone: string; Glyph: LucideIcon; spin?: boolean }> = {
  pending: { tone: 'info', Glyph: Clock },
  queued: { tone: 'info', Glyph: Loader2, spin: true },
  running: { tone: 'accent', Glyph: Loader2, spin: true },
  succeeded: { tone: 'success', Glyph: Check },
  failed: { tone: 'danger', Glyph: XCircle },
  interrupted: { tone: 'warning', Glyph: AlertCircle },
  cancelled: { tone: 'info', Glyph: Ban },
  stopped: { tone: 'warning', Glyph: Ban },
};

function isKnownRunState(value: string): value is KnownRunState {
  return value in RUN_STATES;
}

function StatusBadge({ status }: { status: string | null }) {
  const t = useT();
  if (!status) return <span className="badge">{t('run.idle')}</span>;

  // An unrecognised status still shows as itself rather than as nothing: a
  // status this client has not been taught about is exactly the one worth
  // seeing.
  if (!isKnownRunState(status)) return <span className="badge badge--info">{status}</span>;

  const { tone, Glyph, spin } = RUN_STATES[status];
  return (
    <span className={`badge badge--${tone}`}>
      <Glyph size={10} className={spin ? 'spin' : undefined} />
      {t(`run.status.${status}`)}
    </span>
  );
}

function StepRow({
  step,
  streamText,
  activity,
}: {
  step: RunStep;
  streamText: string | undefined;
  activity: Activity[] | undefined;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);

  // Only the currently-running node streams. Showing it while collapsed is the
  // point: the user should not have to expand a row to watch tokens arrive.
  const live = step.status === 'running' && Boolean(streamText);

  return (
    <li className={`run-step run-step--${step.status}`}>
      <button
        type="button"
        className="run-step__header"
        onClick={() => setOpen((previous) => !previous)}
        aria-expanded={open}
      >
        <ChevronRight size={11} className={`run-step__chevron${open ? ' is-open' : ''}`} />
        <span className="run-step__index">{step.index}</span>
        <span className="run-step__label truncate">{step.node_label || step.node_id}</span>
        <div className="spacer" />
        {step.duration_ms !== null ? (
          <span className="run-step__duration">{formatDuration(step.duration_ms)}</span>
        ) : null}
        <StepIcon status={step.status} />
      </button>

      {/* What the node said it was doing, in its own words. Shown while the row
          is collapsed: "it called the search tool, then read a page" is the
          thing that makes a run feel alive, and hiding it behind a click would
          waste the only account of it the user gets. */}
      {activity && activity.length > 0 ? (
        <ul className="run-step__activity">
          {activity.map((entry, index) => (
            <li
              key={`${entry.kind}-${index}`}
              className={
                'run-step__activity-line' +
                (entry.kind === 'log' && entry.level !== 'info'
                  ? ` run-step__activity-line--${entry.level}`
                  : '')
              }
            >
              {entry.kind === 'tools' ? (
                <>
                  <Wrench size={10} className="run-step__activity-icon" />
                  {entry.step !== null ? `${t('run.activity.round', { n: String(entry.step) })} · ` : ''}
                  {t('run.activity.tools', { tools: entry.tools.join(', ') })}
                </>
              ) : (
                entry.text
              )}
            </li>
          ))}
        </ul>
      ) : null}

      {live ? <pre className="run-step__stream scroll">{streamText}</pre> : null}

      {open ? (
        <div className="run-step__detail">
          {step.error ? (
            <div className="callout callout--error">
              <AlertCircle size={12} className="callout__icon" />
              <span>{step.error}</span>
            </div>
          ) : null}

          {step.inputs ? (
            <JsonViewer value={step.inputs} label={t('run.stepInputs')} defaultDepth={1} maxHeight={220} />
          ) : null}
          {step.outputs ? (
            <JsonViewer value={step.outputs} label={t('run.stepOutputs')} defaultDepth={1} maxHeight={220} />
          ) : null}
          {!step.inputs && !step.outputs && !step.error ? (
            <p className="field__hint">{t('run.noStepData')}</p>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

/**
 * One line of a node's own account of what it did.
 *
 * Two shapes reach the timeline, and they are kept distinct because they read
 * differently: a log is prose written from inside the node, while a tool call
 * is a fact worth an icon.
 */
type Activity =
  | { kind: 'log'; text: string; level: string }
  | { kind: 'tools'; step: number | null; tools: string[] };

/**
 * Group the event stream by node.
 *
 * Chunk events carry the agent node's `agent_step` and `tool_calls`; log events
 * carry `message` and `level` — both built by `compiler.py`, which wires the
 * node's `emit` and `log` callbacks. Every field is read defensively: the
 * payload is `Record<string, unknown>` on the wire, and a node type that grows
 * a new emit must not be able to break the panel.
 */
function buildActivity(events: RunEvent[]): Record<string, Activity[]> {
  const map: Record<string, Activity[]> = {};

  for (const event of events) {
    if (!event.node_id) continue;
    const list = (map[event.node_id] ??= []);

    if (event.type === 'log') {
      const message = event.data.message;
      if (typeof message === 'string' && message) {
        list.push({
          kind: 'log',
          text: message,
          level: typeof event.data.level === 'string' ? event.data.level : 'info',
        });
      }
      continue;
    }

    if (event.type === 'node_output_chunk') {
      const calls = event.data.tool_calls;
      if (Array.isArray(calls) && calls.length > 0) {
        list.push({
          kind: 'tools',
          step: typeof event.data.agent_step === 'number' ? event.data.agent_step : null,
          tools: calls.map((call) => String(call)),
        });
      }
    }
  }

  return map;
}

function StepIcon({ status }: { status: RunStep['status'] }) {
  if (status === 'running') return <Loader2 size={12} className="spin run-step__icon run-step__icon--running" />;
  if (status === 'succeeded') return <Check size={12} className="run-step__icon run-step__icon--ok" />;
  if (status === 'failed') return <XCircle size={12} className="run-step__icon run-step__icon--fail" />;
  return <span className="run-step__icon run-step__icon--skip" />;
}

/**
 * A duration a person reads, not a profiler.
 *
 * Sub-second work reports as "<1s". The figure is only ever weighed against the
 * user's patience, and "320ms" invites reading a precision into it that the
 * comparison does not have. Longer runs get the unit they are felt in.
 */
function formatDuration(ms: number): string {
  if (ms < 1000) return '<1s';
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes}m ${seconds}s`;
}

// ---------------------------------------------------------------------------
// Run inputs
// ---------------------------------------------------------------------------

/**
 * Collects the run's inputs.
 *
 * Prefilled from the Start node's declared `input_schema` so the common case is
 * "edit a value" rather than "remember the field names". The textarea only
 * commits when the JSON parses, which stops a half-typed object from starting a
 * run that fails for a confusing reason.
 */
export function RunDialog({
  initialText,
  busy,
  onRun,
  onClose,
}: {
  initialText: string;
  busy: boolean;
  onRun: (inputs: Record<string, unknown>) => void;
  onClose: () => void;
}) {
  const [text, setText] = useState(initialText);
  const [error, setError] = useState<string | null>(null);
  const t = useT();

  const submit = () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text || '{}');
    } catch (parseError) {
      setError(
        t('common.jsonInvalidDetail', {
          detail: parseError instanceof Error ? parseError.message : '',
        }),
      );
      return;
    }
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
      setError(t('runDialog.objectOnly'));
      return;
    }
    setError(null);
    onRun(parsed as Record<string, unknown>);
  };

  return (
    <div className="run-dialog">
      <p className="field__hint">{t('runDialog.hint')}</p>

      <textarea
        className={`textarea textarea--code${error ? ' textarea--invalid' : ''}`}
        rows={10}
        spellCheck={false}
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          setError(null);
        }}
      />

      {error ? <p className="json-field__error">{error}</p> : null}

      <div className="row">
        <div className="spacer" />
        <button type="button" className="btn" onClick={onClose}>
          {t('runDialog.cancel')}
        </button>
        <button type="button" className="btn btn--primary" disabled={busy} onClick={submit}>
          {busy ? <Loader2 size={12} className="spin" /> : <Play size={12} />}
          {busy ? t('runDialog.starting') : t('runDialog.run')}
        </button>
      </div>
    </div>
  );
}
