/**
 * AI repair, previewed before it touches the canvas.
 *
 * The mechanics match ``GenerateGraphDialog`` — a draft held in local state, an
 * explicit Accept — but the stakes are different, and that shapes the whole
 * component:
 *
 * A generation is judged by taste, so the preview lists what was built. A repair
 * is judged against a *machine check*, which means the cheapest way to satisfy it
 * is to delete whatever the validator dislikes. A preview that only showed the
 * resulting graph would look perfect in exactly that case.
 *
 * So this dialog leads with the **diff**, and deletions in particular: removed
 * nodes are listed in full, in a danger callout, before anything else. The
 * backend's ``regressed`` flag weakens the primary action for the same reason.
 *
 * It also runs on open rather than waiting for a prompt. There is no ambiguity to
 * resolve — the user has already seen the errors and asked for them to be fixed.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Check,
  Loader2,
  Minus,
  Pencil,
  Plus,
  RotateCcw,
  Sparkles,
  Trash2,
  X,
} from 'lucide-react';

import { ApiError, api } from '@/api/client';
import { useT, type MessageKey } from '@/i18n';
import { useWorkflow } from '@/store/workflowStore';
import type { GraphRepairResult } from '@/types/workflow';
import { Dialog } from '@/ui/Dialogs';
import { Icon } from '@/ui/Icon';

/**
 * Human names for the fields a repair can touch on an existing node.
 *
 * A map rather than a template string (`repair.field.${field}`) because the key
 * type is a literal union: a template would widen to `string` and lose the
 * compile-time guarantee that every field has a translation.
 */
const FIELD_KEYS: Record<string, MessageKey> = {
  label: 'repair.field.label',
  description: 'repair.field.description',
  config: 'repair.field.config',
  trusted: 'repair.field.trusted',
};

export function RepairGraphDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const nodeTypes = useWorkflow((state) => state.nodeTypes);
  const replaceGraph = useWorkflow((state) => state.replaceGraph);
  const setError = useWorkflow((state) => state.setError);

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<GraphRepairResult | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [targetCount, setTargetCount] = useState<number | null>(null);
  // Off by default: warnings are advisory, and "fixing" one often means building
  // structure the user had not finished deciding on.
  const [alsoWarnings, setAlsoWarnings] = useState(false);
  // Free text sent with the *next* run. Most useful after a first attempt took
  // the wrong route ("keep my prompts"), which is why it does not trigger a run
  // on every keystroke.
  const [instruction, setInstruction] = useState('');

  // Auto-running an effect is exactly the shape React StrictMode double-invokes
  // in development, and each invocation here is a *paid* model round-trip, so the
  // run is keyed on the settings that should trigger one. The mount pass fires
  // once; flipping the checkbox fires again; StrictMode's duplicate pass is
  // identical and therefore skipped.
  const lastRunKey = useRef<string | null>(null);

  const repair = useCallback(
    async (severity: 'error' | 'all', extraInstruction = '') => {
      setBusy(true);
      setResult(null);
      setLocalError(null);
      try {
        // Read at call time rather than subscribing: the dialog is modal, so the
        // canvas cannot change underneath it, and a subscription would re-fire
        // the request on every unrelated store write.
        const graph = useWorkflow.getState().toGraph();
        const all = useWorkflow.getState().issues;
        setTargetCount(
          severity === 'all'
            ? all.length
            : all.filter((issue) => issue.severity === 'error').length,
        );
        setResult(await api.repairGraph({ graph, severity, instruction: extraInstruction }));
      } catch (error) {
        setLocalError(error instanceof ApiError ? error.message : t('repair.failed'));
      } finally {
        setBusy(false);
      }
    },
    [t],
  );

  useEffect(() => {
    const severity = alsoWarnings ? 'all' : 'error';
    if (lastRunKey.current === severity) return;
    lastRunKey.current = severity;
    void repair(severity);
  }, [repair, alsoWarnings]);

  const apply = () => {
    if (!result) return;
    replaceGraph(result.graph);
    setError(null);
    onClose();
  };

  const removable = result?.diff.nodes_removed ?? [];
  const regressed = result?.regressed ?? false;
  const nothingToFix = result ? !result.changed && result.resolved : false;

  return (
    <Dialog
      title={t('repair.title')}
      wide
      icon={<Sparkles size={14} className="dialog__icon" />}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--ghost" onClick={onClose}>
            {t('repair.cancel')}
          </button>
          <div className="spacer" />
          {result ? (
            <>
              {/*
                A retry has to exist once a result is on screen, otherwise the
                instruction box below it is a dead end: its hint promises the text
                is "sent with the retry", and there would be no retry to press.
                Disabled until there is something to send, so it never reads as a
                no-op button.
              */}
              <button
                type="button"
                className="btn"
                disabled={busy || instruction.trim().length === 0}
                onClick={() => void repair(alsoWarnings ? 'all' : 'error', instruction)}
              >
                {busy ? <Loader2 size={12} className="spin" /> : <RotateCcw size={12} />}
                {t('repair.retry')}
              </button>
              <button type="button" className="btn" onClick={onClose}>
                <X size={12} /> {nothingToFix ? t('repair.done') : t('repair.discard')}
              </button>
              {result.changed ? (
                <button
                  type="button"
                  className={regressed ? 'btn btn--danger' : 'btn btn--primary'}
                  onClick={apply}
                >
                  <Check size={12} /> {t('repair.apply')}
                </button>
              ) : null}
            </>
          ) : (
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy}
              onClick={() => void repair(alsoWarnings ? 'all' : 'error', instruction)}
            >
              {busy ? <Loader2 size={12} className="spin" /> : <Sparkles size={12} />}
              {busy ? t('repair.working') : t('repair.retry')}
            </button>
          )}
        </>
      }
    >
      <div className="generate">
        <p className="field__hint">{t('repair.hint')}</p>

        <label className="checkbox" title={t('repair.alsoWarningsHint')}>
          <input
            type="checkbox"
            checked={alsoWarnings}
            onChange={(event) => setAlsoWarnings(event.target.checked)}
            disabled={busy}
          />
          <span>{t('repair.alsoWarnings')}</span>
        </label>

        {result ? (
          <div className="field">
            <label className="field__label" htmlFor="repair-instruction">
              {t('repair.instruction')}
            </label>
            <textarea
              id="repair-instruction"
              className="textarea"
              rows={2}
              value={instruction}
              placeholder={t('repair.instructionPlaceholder')}
              onChange={(event) => setInstruction(event.target.value)}
              disabled={busy}
            />
            <p className="field__hint">{t('repair.instructionHint')}</p>
          </div>
        ) : null}

        {busy ? (
          <div className="callout callout--info">
            <Loader2 size={13} className="spin callout__icon" />
            <span>
              {targetCount === null
                ? t('repair.starting')
                : t('repair.problemsBefore', { count: targetCount })}
            </span>
          </div>
        ) : null}

        {localError ? (
          <div className="callout callout--warning">
            <AlertCircle size={13} className="callout__icon" />
            <span>{localError}</span>
          </div>
        ) : null}

        {result ? (
          <div className="generate__result">
            <p className="generate__summary">{result.summary}</p>

            <div className="row row--tight">
              <span
                className={`badge badge--${result.resolved ? 'success' : 'danger'}`}
              >
                {result.resolved
                  ? t('repair.fixed', { count: (targetCount ?? 0) + result.remaining })
                  : t('repair.remaining', { count: result.remaining })}
              </span>
              {result.changed ? (
                <span className="badge badge--info">
                  {t('repair.steps', { count: result.attempts })}
                </span>
              ) : null}
            </div>

            {/*
              Warnings come first, and the deleted-node list before even the
              summary of changes. If the model solved the problem by removing the
              user's work, that has to be the first thing read — not a footnote
              under a green "fixed" badge.
            */}
            {result.warnings.map((warning) => (
              <div key={warning} className="callout callout--warning">
                <AlertTriangle size={13} className="callout__icon" />
                <span>{warning}</span>
              </div>
            ))}

            {removable.length > 0 ? (
              <>
                <div className="section-label">{t('repair.removedNodes')}</div>
                <ul className="generate__issues">
                  {removable.map((node) => (
                    <li key={node.node_id} className="callout callout--error">
                      <Trash2 size={12} className="callout__icon" />
                      <span>
                        <strong>{node.label}</strong> <span className="mono dim">{node.type}</span>
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}

            <div className="section-label">{t('repair.whatChanged')}</div>
            <div className="repair__counts">
              <DiffCount
                icon={<Plus size={12} />}
                tone="add"
                count={result.diff.nodes_added.length}
                label={t('repair.added', { count: result.diff.nodes_added.length })}
              />
              <DiffCount
                icon={<Pencil size={12} />}
                tone="change"
                count={result.diff.nodes_updated.length}
                label={t('repair.updated', { count: result.diff.nodes_updated.length })}
              />
              <DiffCount
                icon={<Minus size={12} />}
                tone="remove"
                count={removable.length}
                label={t('repair.removed', { count: removable.length })}
              />
              {/*
                Edges get their own added/removed pair rather than one signed
                delta: "-2 connections" makes the reader do arithmetic to work out
                what is about to happen to their diagram.
              */}
              <DiffCount
                icon={<Plus size={12} />}
                tone="add"
                count={result.diff.edges_added.length}
                label={t('repair.edgesAdded', { count: result.diff.edges_added.length })}
              />
              <DiffCount
                icon={<Minus size={12} />}
                tone="remove"
                count={result.diff.edges_removed.length}
                label={t('repair.edgesRemoved', { count: result.diff.edges_removed.length })}
              />
            </div>

            {!result.changed ? (
              <p className="field__hint">{t('repair.noChange')}</p>
            ) : null}

            {result.diff.nodes_added.length > 0 ? (
              <>
                <div className="section-label">{t('repair.newNodes')}</div>
                <ul className="generate__nodes">
                  {result.diff.nodes_added.map((node) => (
                    <li key={node.node_id} className="generate__node">
                      <span
                        className="generate__node-icon"
                        style={{ color: nodeTypes[node.type]?.color }}
                      >
                        <Icon name={nodeTypes[node.type]?.icon ?? 'box'} size={13} />
                      </span>
                      <div className="generate__node-text">
                        <div className="generate__node-head">
                          <strong>{node.label}</strong>
                          <span className="badge">{node.type}</span>
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}

            {result.diff.nodes_updated.length > 0 ? (
              <>
                <div className="section-label">{t('repair.changedNodes')}</div>
                <ul className="generate__edges">
                  {result.diff.nodes_updated.map((node) => (
                    <li key={node.node_id}>
                      <strong>{node.label}</strong>
                      <span className="dim">
                        {node.fields
                          .map((field) => (FIELD_KEYS[field] ? t(FIELD_KEYS[field]) : field))
                          .join(' · ')}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}

            {!result.validation.ok || result.validation.issues.length > 0 ? (
              <>
                <div className="section-label">{t('repair.remainingProblems')}</div>
                <ul className="generate__issues">
                  {result.validation.issues.map((issue, index) => (
                    <li
                      key={`${issue.code}-${index}`}
                      className={`callout callout--${issue.severity === 'error' ? 'error' : 'warning'}`}
                    >
                      <AlertCircle size={12} className="callout__icon" />
                      <span>{issue.message}</span>
                    </li>
                  ))}
                </ul>
              </>
            ) : null}

            {result.transcript.length > 0 ? (
              <details className="generate__transcript">
                <summary>{t('gen.toolCalls', { count: result.transcript.length })}</summary>
                <ul>
                  {result.transcript.map((entry, index) => (
                    <li key={index} className="mono">
                      <strong>{entry.tool}</strong> {JSON.stringify(entry.arguments)}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </div>
        ) : null}
      </div>
    </Dialog>
  );
}

/** One tally in the change summary. `tone` drives the colour only. */
function DiffCount({
  icon,
  tone,
  count,
  label,
}: {
  icon: React.ReactNode;
  tone: 'add' | 'remove' | 'change';
  count: number;
  label: string;
}) {
  return (
    <span className={`repair__count repair__count--${tone}${count === 0 ? ' is-zero' : ''}`}>
      {icon}
      {label}
    </span>
  );
}
