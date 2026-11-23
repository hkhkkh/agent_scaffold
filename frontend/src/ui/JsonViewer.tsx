/**
 * Collapsible JSON tree, plus copy-to-clipboard.
 *
 * Run output is the main thing a user inspects here, and run output is *not*
 * shaped for display: it nests arbitrarily, contains long strings (a model's
 * reply), and is frequently re-rendered while a run streams. So the viewer keeps
 * its own expand state per node rather than remounting on every value change —
 * otherwise a collapsing branch would spring back open on the next event.
 */

import { useMemo, useState } from 'react';
import { Check, ChevronRight, Copy } from 'lucide-react';

import { useT } from '@/i18n';

type Kind = 'null' | 'undefined' | 'string' | 'number' | 'boolean' | 'array' | 'object';

function kindOf(value: unknown): Kind {
  if (value === null) return 'null';
  if (value === undefined) return 'undefined';
  if (Array.isArray(value)) return 'array';
  const type = typeof value;
  if (type === 'string') return 'string';
  if (type === 'number') return 'number';
  if (type === 'boolean') return 'boolean';
  if (type === 'object') return 'object';
  return 'string';
}

function safeStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    // Circular structures are possible when a node returns a live object graph.
    return String(value);
  }
}

export function JsonViewer({
  value,
  label,
  defaultDepth = 1,
  maxHeight,
}: {
  value: unknown;
  label?: string;
  /** How many levels start expanded. 0 means everything is collapsed. */
  defaultDepth?: number;
  maxHeight?: number;
}) {
  const t = useT();
  const text = useMemo(() => safeStringify(value), [value]);
  const [copied, setCopied] = useState(false);

  const copy = () => {
    void navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true);
        window.setTimeout(() => setCopied(false), 1200);
      })
      .catch(() => {
        // Clipboard access can be denied; the text is on screen regardless.
      });
  };

  return (
    <div className="json-viewer">
      <div className="json-viewer__bar">
        {label ? <span className="json-viewer__label">{label}</span> : null}
        <div className="spacer" />
        <button
          type="button"
          className="btn btn--ghost btn--icon btn--sm"
          onClick={copy}
          title={copied ? t('common.copied') : t('common.copy')}
        >
          {copied ? <Check size={11} /> : <Copy size={11} />}
        </button>
      </div>

      <div
        className="json-viewer__body mono scroll"
        style={maxHeight ? { maxHeight } : undefined}
      >
        <JsonNode value={value} name={null} depth={0} defaultDepth={defaultDepth} />
      </div>
    </div>
  );
}

function JsonNode({
  value,
  name,
  depth,
  defaultDepth,
}: {
  value: unknown;
  name: string | null;
  depth: number;
  defaultDepth: number;
}) {
  const [open, setOpen] = useState(depth < defaultDepth);

  const kind = kindOf(value);
  const expandable = kind === 'object' || kind === 'array';

  const entries = useMemo<[string, unknown][]>(() => {
    if (kind === 'object') return Object.entries(value as Record<string, unknown>);
    if (kind === 'array') return (value as unknown[]).map((item, index) => [String(index), item]);
    return [];
  }, [kind, value]);

  if (!expandable) {
    return (
      <div className="json-viewer__row" style={indent(depth)}>
        {name !== null ? <span className="json-viewer__key">{name}</span> : null}
        <Primitive value={value} kind={kind} />
      </div>
    );
  }

  return (
    <div className="json-viewer__group">
      <button
        type="button"
        className="json-viewer__toggle"
        style={indent(depth)}
        onClick={() => setOpen((previous) => !previous)}
        aria-expanded={open}
      >
        <ChevronRight
          size={11}
          className={`json-viewer__chevron${open ? ' is-open' : ''}`}
        />
        {name !== null ? <span className="json-viewer__key">{name}</span> : null}
        <span className="json-viewer__summary">{summarise(kind, entries.length)}</span>
      </button>

      {open ? (
        <div className="json-viewer__children">
          {entries.length === 0 ? (
            <div className="json-viewer__row json-viewer__empty" style={indent(depth + 1)}>
              empty
            </div>
          ) : (
            entries.map(([key, item]) => (
              <JsonNode
                // Index keys are correct here: array order *is* the identity.
                key={key}
                value={item}
                name={key}
                depth={depth + 1}
                defaultDepth={defaultDepth}
              />
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

function Primitive({ value, kind }: { value: unknown; kind: Kind }) {
  if (kind === 'string') {
    return <span className="json-viewer__value json-viewer__value--string">{`"${value as string}"`}</span>;
  }
  if (kind === 'null' || kind === 'undefined') {
    return <span className="json-viewer__value json-viewer__value--null">{kind}</span>;
  }
  return (
    <span className={`json-viewer__value json-viewer__value--${kind}`}>{String(value)}</span>
  );
}

function summarise(kind: Kind, count: number): string {
  if (kind === 'array') return count === 0 ? '[ ]' : `[ ${count} ]`;
  return count === 0 ? '{ }' : `{ ${count} }`;
}

function indent(depth: number): { paddingLeft: number } {
  return { paddingLeft: depth * 14 };
}
