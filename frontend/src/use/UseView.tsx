/**
 * Use view — **one box, for every agent**.
 *
 * The interface does not change per workflow. What a graph declares in its Start
 * node is the graph's business, not the user's: someone who wants a refund says
 * so in a sentence, and whatever the agent still needs it asks for — that is what
 * a Human Input node is for. Rendering those declarations as a form (which this
 * view used to do) put the user in the position of filling in the agent's own
 * parameters, and made every agent look different. A platform whose whole claim
 * is "one mould, every agent" cannot have a bespoke front end per agent.
 *
 * What is on screen comes from the run's own event stream. Everything a user
 * wants to see is already in it: nodes log their reasoning and every tool call
 * (`agent.py` logs `Step N: calling http_request(...)`), loops log each pass,
 * retrieval logs what it matched, and the answer arrives on `run_finished`.
 * The front end used to drop all of that on the floor.
 */

import { useEffect, useMemo, useState } from 'react';
import { Loader2, Play, Send } from 'lucide-react';

import type { InterruptPayload, RunEvent } from '@/types/workflow';
import { finalOutput } from '@/run/finalOutput';
import { beginRun, watchRun } from '@/run/runController';
import { useWorkflow } from '@/store/workflowStore';
import { inputsForText } from './inputs';
import { useT } from '@/i18n';

interface Line {
  id: string;
  text: string;
  /** Warnings and errors are worth setting apart from the running commentary. */
  warn: boolean;
}

/**
 * Turn the event stream into the agent's side of the conversation.
 *
 * Only `log` events become lines. `node_started` / `node_finished` are progress,
 * not speech — showing them would bury the reasoning under bookkeeping, and the
 * nodes that have something to say already say it through `ctx.log`.
 */
/**
 * Engineering telemetry belongs in the run panel, not in the user's
 * conversation: "Agent used 4072 tokens" is not something an end user asked to
 * read, and an English warning about template resolution is actively alarming.
 */
const INTERNAL_LOG = /^\[(warning|error)\]/i;
const TELEMETRY_LOG =
  /^(Start resolved\b|Agent (started|finished|used)\b|End accepted result\b|Step \d+: calling\b|.*did not resolve; sent the context value instead)/i;

export function buildLines(events: RunEvent[]): Line[] {
  const out: Line[] = [];
  for (const event of events) {
    if (event.type !== 'log') continue;
    const text = typeof event.data.message === 'string' ? event.data.message : '';
    if (!text) continue;
    if (INTERNAL_LOG.test(text) || TELEMETRY_LOG.test(text)) continue;
    const level = typeof event.data.level === 'string' ? event.data.level : 'info';
    out.push({
      id: `log-${event.seq}`,
      text,
      warn: level === 'warning' || level === 'error',
    });
  }
  return out;
}

/** An answer may be a string, a number, or a structure. Show whichever it is. */
function answerText(value: unknown): string {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Statuses a run will still move out of. Mirrors `runController`'s guard. */
const IN_FLIGHT = new Set(['pending', 'queued', 'running']);

export function UseView() {
  const t = useT();
  const runId = useWorkflow((state) => state.runId);
  const runStatus = useWorkflow((state) => state.runStatus);
  const runEvents = useWorkflow((state) => state.runEvents);
  const runOutputs = useWorkflow((state) => state.runOutputs);
  const runError = useWorkflow((state) => state.runError);
  const runInterrupt = useWorkflow((state) => state.runInterrupt);
  const cancelRun = useWorkflow((state) => state.cancelRun);

  const [asked, setAsked] = useState('');
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(false);

  const lines = useMemo(() => buildLines(runEvents), [runEvents]);
  const answer = finalOutput(runOutputs);
  const running = IN_FLIGHT.has(runStatus ?? '');
  const waiting = runStatus === 'interrupted' && runInterrupt !== null;

  // Same watchdog the editor uses, so a dropped frame settles here too.
  useEffect(() => {
    if (!running || !runId) return;
    return watchRun(runId);
  }, [running, runId]);

  const send = async () => {
    const text = draft.trim();
    if (!text || running) return;
    setAsked(text);
    setDraft('');
    setBusy(true);
    // One box for every agent, but the Start node still owns the contract: a
    // workflow that declares `message` must receive `message`, not a fixed
    // `query` that its prompt template never reads.
    await beginRun(inputsForText(useWorkflow.getState().nodes, text));
    setBusy(false);
  };

  const started = asked !== '' || runId !== null;

  return (
    <div className="run">
      <div className="run__body">
        {started ? (
          <div className="chat">
            {asked ? <p className="chat__said">{asked}</p> : null}

            {lines.map((line) => (
              <p key={line.id} className={`chat__agent${line.warn ? ' chat__agent--warn' : ''}`}>
                {line.text}
              </p>
            ))}

            {waiting ? (
              <AskCard
                key={runInterrupt?.__interrupt_id ?? runId ?? 'ask'}
                interrupt={runInterrupt as InterruptPayload}
                onCancel={() => void cancelRun()}
              />
            ) : null}

            {answer ? (
              <div className="run__outputs">
                <span className="section-label">{t('use.answer', { key: answer.key })}</span>
                <div style={{ whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>
                  {answerText(answer.value)}
                </div>
              </div>
            ) : null}

            {runError && runStatus !== 'cancelled' ? (
              <div className="callout callout--danger">
                <span>{runError}</span>
              </div>
            ) : null}

            {runStatus === 'cancelled' ? (
              <p className="field__hint">{t('run.cancelledNote')}</p>
            ) : null}
          </div>
        ) : (
          <p className="empty">{t('use.opening')}</p>
        )}
      </div>

      <div className="chat__composer">
        <input
          className="input"
          value={draft}
          placeholder={t('use.prompt')}
          disabled={running || waiting}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && !event.shiftKey) {
              event.preventDefault();
              void send();
            }
          }}
        />
        <button
          type="button"
          className="btn btn--primary"
          disabled={busy || running || waiting || draft.trim() === ''}
          onClick={() => void send()}
        >
          {busy || running ? <Loader2 size={12} className="spin" /> : <Send size={12} />}
          {running ? t('app.running') : t('use.send')}
        </button>
      </div>
    </div>
  );
}

/**
 * The agent asking for something it could not work out itself.
 *
 * Inline in the conversation rather than a modal: the question is part of the
 * exchange, and a dialog that covers the reasoning the user is watching reads as
 * an interruption instead of a turn.
 */
function AskCard({
  interrupt,
  onCancel,
}: {
  interrupt: InterruptPayload;
  onCancel: () => void;
}) {
  const t = useT();
  const resumeRun = useWorkflow((state) => state.resumeRun);
  const fields = useMemo(() => Object.entries(interrupt.fields ?? {}), [interrupt.fields]);

  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const initial: Record<string, unknown> = {};
    for (const [name, spec] of Object.entries(interrupt.fields ?? {})) {
      initial[name] = spec.type === 'boolean' ? false : '';
    }
    return initial;
  });
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await resumeRun({ approved: true, values });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat__ask">
      <p className="chat__agent chat__agent--ask">
        {interrupt.message?.trim() ? interrupt.message : t('hitl.input')}
      </p>

      {fields.length === 0 ? (
        <p className="field__hint">{t('hitl.noFields')}</p>
      ) : (
        fields.map(([name, spec]) => (
          <label key={name} className="field">
            <span className="field__label">{spec.label ?? name}</span>
            {spec.type === 'boolean' ? (
              <input
                type="checkbox"
                checked={Boolean(values[name])}
                onChange={(event) =>
                  setValues((previous) => ({ ...previous, [name]: event.target.checked }))
                }
              />
            ) : (
              <input
                className="input"
                value={String(values[name] ?? '')}
                onChange={(event) =>
                  setValues((previous) => ({ ...previous, [name]: event.target.value }))
                }
                onKeyDown={(event) => {
                  if (event.key === 'Enter') {
                    event.preventDefault();
                    void submit();
                  }
                }}
              />
            )}
          </label>
        ))
      )}

      <div className="row">
        <button type="button" className="btn btn--primary" disabled={busy} onClick={() => void submit()}>
          {busy ? <Loader2 size={12} className="spin" /> : <Play size={12} />} {t('hitl.approve')}
        </button>
        <button type="button" className="btn btn--ghost" disabled={busy} onClick={onCancel}>
          {t('hitl.cancelRun')}
        </button>
      </div>
    </div>
  );
}
