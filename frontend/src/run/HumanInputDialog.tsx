/**
 * Human-in-the-loop review dialog.
 *
 * Shown whenever a run suspends on a Human Input node. The payload is authored
 * by the *backend node*, not by this component: the node decides the message,
 * which fields to collect, and whether rejection is allowed. That keeps the
 * contract in one place — a workflow author changing the node's `fields` param
 * changes this form without a frontend edit.
 */

import { useMemo, useState } from 'react';
import { Ban, Check, ChevronRight, Loader2, UserCheck } from 'lucide-react';

import { useWorkflow } from '@/store/workflowStore';
import type { InterruptPayload } from '@/types/workflow';
import { useT } from '@/i18n';
import { Dialog } from '@/ui/Dialogs';
import { JsonViewer } from '@/ui/JsonViewer';

export function HumanInputDialog() {
  const interrupt = useWorkflow((state) => state.runInterrupt);
  const runStatus = useWorkflow((state) => state.runStatus);
  const resumeRun = useWorkflow((state) => state.resumeRun);
  const cancelRun = useWorkflow((state) => state.cancelRun);

  if (!interrupt || runStatus !== 'interrupted') return null;

  return (
    <HumanInputForm
      interrupt={interrupt}
      onResume={(payload) => void resumeRun(payload)}
      onCancel={() => void cancelRun()}
    />
  );
}

function HumanInputForm({
  interrupt,
  onResume,
  onCancel,
}: {
  interrupt: InterruptPayload;
  onResume: (payload: Record<string, unknown>) => void;
  onCancel: () => void;
}) {
  const fields = useMemo(() => Object.entries(interrupt.fields ?? {}), [interrupt.fields]);
  const t = useT();

  // Values are keyed by field name; a boolean is `false` until touched, which is
  // why initialisation happens here rather than being inferred at submit time.
  const [values, setValues] = useState<Record<string, unknown>>(() => {
    const initial: Record<string, unknown> = {};
    for (const [name, spec] of Object.entries(interrupt.fields ?? {})) {
      initial[name] = spec.type === 'boolean' ? false : '';
    }
    return initial;
  });
  const [busy, setBusy] = useState(false);

  const submit = async (approved: boolean) => {
    setBusy(true);
    try {
      await onResume({ approved, values });
    } finally {
      setBusy(false);
    }
  };

  const allowReject = interrupt.allow_reject !== false;
  const title = interrupt.message?.trim() ? t('hitl.review') : t('hitl.input');

  return (
    <Dialog
      title={title}
      icon={<UserCheck size={14} className="dialog__icon" />}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn btn--ghost" onClick={onCancel} disabled={busy}>
            {t('hitl.cancelRun')}
          </button>
          <div className="spacer" />
          {allowReject ? (
            <button type="button" className="btn btn--danger" disabled={busy} onClick={() => submit(false)}>
              {busy ? <Loader2 size={12} className="spin" /> : <Ban size={12} />} {t('hitl.reject')}
            </button>
          ) : null}
          <button type="button" className="btn btn--primary" disabled={busy} onClick={() => submit(true)}>
            {busy ? <Loader2 size={12} className="spin" /> : <Check size={12} />} {t('hitl.approve')}
          </button>
        </>
      }
    >
      {/* The message is written by whoever authored the workflow, so it leads as
          the agent speaking rather than as a system prompt. The review of the
          old dialog was that it read like a ticket form; letting the node's own
          words come first is the fix. */}
      {interrupt.message?.trim() ? (
        <p className="chat__agent chat__agent--ask hitl__ask">{interrupt.message}</p>
      ) : null}

      {fields.length === 0 ? (
        <p className="field__hint">{t('hitl.noFields')}</p>
      ) : (
        <div className="chat__ask">
          <p className="field__hint">{t('hitl.needs')}</p>
          <div className="hitl__fields">
            {fields.map(([name, spec]) => (
              <HitlField
                key={name}
                name={name}
                spec={spec}
                value={values[name]}
                setValue={(value) => setValues((previous) => ({ ...previous, [name]: value }))}
              />
            ))}
          </div>
        </div>
      )}

      {/* The upstream value is evidence, not an instruction — collapsed and last,
          because the ask is the only thing the user has to act on. */}
      {interrupt.context !== null && interrupt.context !== undefined ? (
        <HitlContext value={interrupt.context} />
      ) : null}
    </Dialog>
  );
}

/**
 * The state the run held when it paused, folded away.
 *
 * A workflow author chasing a badly worded question wants it; the person being
 * asked does not. The run panel's raw output gets the same treatment: present,
 * collapsed, and last.
 */
function HitlContext({ value }: { value: unknown }) {
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
        <span>{t('hitl.context')}</span>
        <span className="dim">{t('hitl.contextHint')}</span>
      </button>
      {open ? (
        <div className="run__debug-body">
          <JsonViewer value={value} defaultDepth={1} maxHeight={200} />
        </div>
      ) : null}
    </div>
  );
}

function HitlField({
  name,
  spec,
  value,
  setValue,
}: {
  name: string;
  spec: { type?: string; label?: string; description?: string };
  value: unknown;
  setValue: (value: unknown) => void;
}) {
  const label = spec.label ?? name;
  const type = spec.type ?? 'string';
  const id = `hitl-${name}`;
  const t = useT();

  return (
    <div className="field">
      <label className="field__label" htmlFor={id}>
        {label}
      </label>

      {type === 'boolean' ? (
        <label className="checkbox">
          <input
            id={id}
            type="checkbox"
            checked={Boolean(value)}
            onChange={(event) => setValue(event.target.checked)}
          />
          <span>{Boolean(value) ? t('hitl.yes') : t('hitl.no')}</span>
        </label>
      ) : type === 'number' ? (
        <input
          id={id}
          className="input"
          type="number"
          value={typeof value === 'number' ? value : ''}
          // An empty box must not become 0 — it means "not supplied yet".
          onChange={(event) => setValue(event.target.value === '' ? '' : Number(event.target.value))}
        />
      ) : type === 'json' || type === 'object' ? (
        <textarea
          id={id}
          className="textarea textarea--code"
          rows={4}
          spellCheck={false}
          value={typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)}
          onChange={(event) => setValue(event.target.value)}
        />
      ) : type === 'text' || type === 'multiline' ? (
        <textarea
          id={id}
          className="textarea"
          rows={4}
          value={String(value ?? '')}
          onChange={(event) => setValue(event.target.value)}
        />
      ) : (
        <input
          id={id}
          className="input"
          value={String(value ?? '')}
          onChange={(event) => setValue(event.target.value)}
        />
      )}

      {spec.description ? <p className="field__hint">{spec.description}</p> : null}
    </div>
  );
}
