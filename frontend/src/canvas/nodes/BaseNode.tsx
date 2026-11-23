/**
 * The custom node card.
 *
 * This is the surface the whole product is built around, so it carries more than
 * an icon and a title:
 *
 *  - **The description is first-class.** It is the text the user writes to say
 *    what the step does, it is what the AI reads when filling the node in, and
 *    it is editable directly on the card. Putting it behind a properties panel
 *    would hide the single most important field in the app.
 *  - **Ports are coloured by data type**, matching the backend's palette, so a
 *    mis-typed connection is visible before it is attempted.
 *  - **Run status is shown in place**, because "which node is stuck" is the
 *    first question when a run misbehaves.
 */

import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import {
  AlertTriangle,
  Check,
  CircleSlash,
  Loader2,
  RotateCcw,
  Sparkles,
  XCircle,
  Zap,
} from 'lucide-react';

import type { AssistNote } from '@/assist/auto';
import { useT } from '@/i18n';
import type {
  CanvasNode,
  NodeOutputSummary,
  NodeRunStatus,
} from '@/store/workflowStore';
import { Icon } from '@/ui/Icon';

/** Input rows are laid out at a fixed pitch so handles line up with labels. */
const PORT_PITCH = 22;
const PORT_TOP = 42;

export const BaseNode = memo(function BaseNode({ data, selected, id }: NodeProps<CanvasNode>) {
  const {
    spec,
    inputs,
    outputs,
    runStatus,
    runWarnings,
    runOutput,
    assistNote,
    streamText,
    loopProgress,
    errorCount,
    color,
    icon,
  } = data;
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draftDescription, setDraftDescription] = useState(spec.description);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Track external changes (e.g. an accepted AI suggestion) without clobbering
  // what the user is currently typing.
  useEffect(() => {
    if (!editing) setDraftDescription(spec.description);
  }, [spec.description, editing]);

  useEffect(() => {
    if (!editing) return;
    const element = textareaRef.current;
    if (!element) return;
    element.focus();
    element.setSelectionRange(element.value.length, element.value.length);
  }, [editing]);

  // Node cards dispatch intent rather than importing the store, so this stays a
  // presentational component that is cheap to render and easy to test.
  const emitNodePatch = useCallback(
    (patch: Record<string, unknown>) => {
      window.dispatchEvent(
        new CustomEvent('scaffold:update-node', { detail: { id, patch } }),
      );
    },
    [id],
  );

  const commitDescription = useCallback(() => {
    setEditing(false);
    const trimmed = draftDescription.trim();
    if (trimmed !== spec.description) emitNodePatch({ description: trimmed });
  }, [draftDescription, emitNodePatch, spec.description]);

  const commitLabel = useCallback(
    (value: string) => {
      const trimmed = value.trim();
      if (trimmed && trimmed !== spec.label) emitNodePatch({ label: trimmed });
    },
    [emitNodePatch, spec.label],
  );

  const openInspector = useCallback(() => {
    window.dispatchEvent(new CustomEvent('scaffold:select-node', { detail: { id } }));
  }, [id]);

  return (
    <div
      className={[
        'node',
        selected ? 'node--selected' : '',
        runStatus ? `node--${runStatus}` : '',
      ]
        .filter(Boolean)
        .join(' ')}
      style={{ '--node-color': color } as React.CSSProperties}
      onDoubleClick={openInspector}
    >
      <div className="node__ports node__ports--in">
        {inputs.map((port, index) => (
          <div
            key={port.id}
            className="node__port"
            style={{ top: PORT_TOP + index * PORT_PITCH }}
          >
            <Handle
              type="target"
              id={port.id}
              position={Position.Left}
              className="port"
              style={{ background: portColor(port.type) }}
              data-required={port.required ? 'true' : undefined}
              title={`${port.label} (${port.type})${port.required ? ` · ${t('inspector.required')}` : ''}`}
            />
            <span className="node__port-label">{port.label}</span>
          </div>
        ))}
      </div>

      <div className="node__ports node__ports--out">
        {outputs.map((port, index) => (
          <div
            key={port.id}
            className="node__port node__port--right"
            style={{ top: PORT_TOP + index * PORT_PITCH }}
          >
            <span className="node__port-label">{port.label}</span>
            <Handle
              type="source"
              id={port.id}
              position={Position.Right}
              className="port"
              style={{ background: portColor(port.type) }}
              title={`${port.label} (${port.type})`}
            />
          </div>
        ))}
      </div>

      <header className="node__header">
        <span className="node__icon">
          <Icon name={icon} size={13} />
        </span>

        <input
          className="node__title nodrag"
          // `key` remounts the input when the label changes externally, which is
          // simpler and less error-prone than syncing a controlled value.
          key={spec.label}
          defaultValue={spec.label}
          onBlur={(event) => {
            const trimmed = event.target.value.trim();
            // An empty title is not a name. Put the old one back in the box:
            // showing blank while the document still has a label reads as if
            // the rename worked, but nothing was saved.
            if (!trimmed) {
              event.target.value = spec.label;
              return;
            }
            commitLabel(trimmed);
          }}
          onKeyDown={(event) => {
            if (event.key === 'Enter') event.currentTarget.blur();
            if (event.key === 'Escape') {
              event.currentTarget.value = spec.label;
              event.currentTarget.blur();
            }
          }}
          aria-label={t('node.label')}
        />

        {errorCount ? (
          <span
            className="node__error"
            title={t(errorCount === 1 ? 'canvas.issues.one' : 'canvas.issues.many', {
              count: errorCount,
            })}
          >
            <AlertTriangle size={10} />
          </span>
        ) : null}

        {runStatus ? <NodeStatus status={runStatus} warning={runWarnings?.[0]} /> : null}
      </header>

      <div className="node__meta">
        <span className="node__type">{spec.type}</span>
        {loopProgress ? (
          <span
            className={`badge badge--loop${
              loopProgress.continuing ? ' badge--loop-active' : ''
            }`}
            title={t('node.loop.round', {
              iteration: loopProgress.iteration,
              max: loopProgress.max ?? '?',
            })}
          >
            <RotateCcw size={9} />{' '}
            {t('node.loop.round', {
              iteration: loopProgress.iteration,
              max: loopProgress.max ?? '?',
            })}
          </span>
        ) : null}
        {runOutput ? <OutputChip summary={runOutput} /> : null}
      </div>

      {editing ? (
        <textarea
          ref={textareaRef}
          className="node__description node__description--editing nodrag"
          value={draftDescription}
          rows={3}
          placeholder={t('node.editPlaceholder')}
          onChange={(event) => setDraftDescription(event.target.value)}
          onBlur={commitDescription}
          onKeyDown={(event) => {
            if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
              event.preventDefault();
              commitDescription();
            }
            if (event.key === 'Escape') {
              setDraftDescription(spec.description);
              setEditing(false);
            }
            // Without this, React Flow reads keystrokes as canvas shortcuts.
            event.stopPropagation();
          }}
        />
      ) : (
        <button
          type="button"
          className={`node__description nodrag ${
            spec.description ? '' : 'node__description--empty'
          }`}
          onClick={() => setEditing(true)}
          title={t('node.editHint')}
        >
          {spec.description || t('node.describe')}
        </button>
      )}

      {assistNote ? <AssistChip note={assistNote} /> : null}

      {streamText ? (
        <div className="node__stream" aria-live="polite">
          {streamText}
        </div>
      ) : null}

      {spec.trusted ? (
        <div className="node__flags">
          <span className="badge badge--warning" title={t('node.trustedTooltip')}>
            <Zap size={9} /> {t('node.trustedBadge')}
          </span>
        </div>
      ) : null}
    </div>
  );
});

/**
 * The status glyph, resolved from tables rather than a chain of conditionals —
 * the same shape the node types themselves use. Adding an outcome should be a
 * row here, not a new branch in every place that reads a status.
 */
const STATUS_GLYPH = {
  pending: Loader2,
  running: Loader2,
  succeeded: Check,
  failed: XCircle,
  skipped: CircleSlash,
  warn: AlertTriangle,
} as const;

/** CSS suffix per status. `pending` and `skipped` are deliberately quiet. */
const STATUS_TONE: Record<NodeRunStatus | 'warn', string> = {
  pending: 'pending',
  running: 'running',
  succeeded: 'ok',
  failed: 'fail',
  skipped: 'skipped',
  warn: 'warn',
};

/**
 * What the node handed on, as a size rather than as a value.
 *
 * The card cannot show the data itself — and should not try, since the raw value
 * is in the run panel for whoever needs it. It can show whether anything came
 * out, which is the question a green tick fails to answer: an empty output is
 * drawn in amber, because "it ran" and "it did something" are different claims.
 */
function OutputChip({ summary }: { summary: NodeOutputSummary }) {
  const t = useT();
  if (summary.kind === 'empty') {
    return (
      <span className="node__output node__output--empty">{t('node.output.empty')}</span>
    );
  }
  return (
    <span className="node__output">
      {t(summary.kind === 'text' ? 'node.output.text' : 'node.output.fields', {
        n: String(summary.size),
      })}
    </span>
  );
}

/**
 * What the assistant did to this card, when it acted without being asked.
 *
 * It has to be visible. Auto mode changes the document on its own, and a change
 * nobody can see is indistinguishable from a bug — the user edits a field, it
 * comes back different, and there is nothing on screen to explain why. Naming
 * the fields is what makes it an answer rather than a hint.
 *
 * A failure says so too. Silence would leave the user with a node that looks
 * untouched and no reason to think anything was ever attempted.
 */
function AssistChip({ note }: { note: AssistNote }) {
  const t = useT();
  if (note.failed) {
    return (
      <div className="node__assist node__assist--failed" title={t('assist.failedHint')}>
        <AlertTriangle size={9} /> {t('assist.failed')}
      </div>
    );
  }
  const fields = note.keys.join(', ');
  return (
    <div
      className="node__assist"
      title={t('assist.filledFields', { fields })}
    >
      <Sparkles size={9} /> {t('assist.filled')}
    </div>
  );
}

function NodeStatus({ status, warning }: { status: NodeRunStatus; warning?: string }) {
  const t = useT();

  // A warning replaces the success glyph rather than sitting beside it. A step
  // that succeeded *and* said something was wrong is the one worth looking at,
  // and the check mark is exactly what stops anyone from looking.
  const shown: NodeRunStatus | 'warn' = warning && status === 'succeeded' ? 'warn' : status;
  const Glyph = STATUS_GLYPH[shown];
  // Only a live node spins: a pending node reuses the loader glyph because it is
  // the same "not done" idea, but frozen it reads as an outline rather than as
  // work in progress.
  const spinning = shown === 'running';

  return (
    <span
      className={`node__status node__status--${STATUS_TONE[shown]}`}
      title={warning ?? t(`node.status.${status}`)}
    >
      <Glyph size={11} className={spinning ? 'spin' : undefined} />
    </span>
  );
}

// ---------------------------------------------------------------------------
// Port colours
// ---------------------------------------------------------------------------

/**
 * Fallback port palette.
 *
 * The backend sends the authoritative map at `/api/node-types`; this is what is
 * used on first paint and if that request fails.
 */
const PORT_COLORS: Record<string, string> = {
  any: '#94a3b8',
  text: '#38bdf8',
  message: '#818cf8',
  messages: '#6366f1',
  language_model: '#e879f9',
  tool: '#2dd4bf',
  retriever: '#34d399',
  memory: '#fb923c',
  json: '#f87171',
  table: '#f472b6',
  embeddings: '#4ade80',
  number: '#fbbf24',
  boolean: '#a3e635',
};

export function portColor(type: string): string {
  return PORT_COLORS[type] ?? '#94a3b8';
}

