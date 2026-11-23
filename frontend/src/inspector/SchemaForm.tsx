/**
 * Generic parameter form.
 *
 * Renders an Inspector panel for **any** node type directly from the backend's
 * `ParamSpec` list. There is deliberately no per-node-type form component: a new
 * node type added on the backend gets a working editor with zero frontend
 * changes, and there is exactly one place where "how does a boolean render" is
 * decided.
 *
 * Two behaviours worth calling out:
 *
 *  - **`show_if` conditional visibility.** Fields appear only when a sibling
 *    value matches, so a node with mutually exclusive modes shows only the
 *    relevant settings instead of a wall of irrelevant ones.
 *  - **JSON fields keep a local draft.** A half-typed `{` is not valid JSON, and
 *    committing on every keystroke would either throw or silently clear the
 *    field. The draft is committed only once it parses.
 */

import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, ChevronRight } from 'lucide-react';

import { useT } from '@/i18n';
import { TemplateTextarea } from '@/inspector/TemplateTextarea';
import type { TemplateCandidate } from '@/inspector/templateCompletions';
import type { ParamSpec } from '@/types/workflow';

export interface SchemaFormProps {
  params: ParamSpec[];
  config: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
  /** Field names to highlight as invalid. */
  invalidFields?: Set<string>;
  /**
   * Paths offered by `{{ }}` completion. Built by the Inspector from the graph;
   * optional so every other caller of this presentational form still works.
   */
  templateCandidates?: TemplateCandidate[];
}

export function SchemaForm({
  params,
  config,
  onChange,
  invalidFields,
  templateCandidates,
}: SchemaFormProps) {
  const t = useT();
  const visible = useMemo(
    () => params.filter((param) => isVisible(param, config)),
    [params, config],
  );

  const primary = visible.filter((param) => !param.advanced);
  const advanced = visible.filter((param) => param.advanced);

  return (
    <div className="schema-form">
      {primary.length === 0 && advanced.length === 0 ? (
        <p className="empty">{t('schema.empty')}</p>
      ) : null}

      {primary.map((param) => (
        <ParamField
          key={param.name}
          param={param}
          value={config[param.name]}
          invalid={invalidFields?.has(param.name)}
          templateCandidates={templateCandidates}
          onChange={(value) => onChange(param.name, value)}
        />
      ))}

      {advanced.length > 0 ? (
        <details className="schema-form__advanced">
          <summary>
            <ChevronRight size={12} className="schema-form__chevron" />
            {t('schema.advanced', { count: advanced.length })}
          </summary>
          <div className="schema-form__advanced-body">
            {advanced.map((param) => (
              <ParamField
                key={param.name}
                param={param}
                value={config[param.name]}
                invalid={invalidFields?.has(param.name)}
                templateCandidates={templateCandidates}
                onChange={(value) => onChange(param.name, value)}
              />
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Visibility
// ---------------------------------------------------------------------------

/**
 * Evaluate a param's `show_if` against the current config.
 *
 * An absent condition means always visible; a condition on a field that is
 * itself unset hides the dependent field, which is the behaviour that keeps
 * mode-specific settings out of the way until a mode is chosen.
 */
export function isVisible(param: ParamSpec, config: Record<string, unknown>): boolean {
  const condition = param.show_if;
  if (!condition) return true;
  return condition.equals.some((value) => config[condition.field] === value);
}

// ---------------------------------------------------------------------------
// Field dispatch
// ---------------------------------------------------------------------------

function ParamField({
  param,
  value,
  invalid,
  templateCandidates,
  onChange,
}: {
  param: ParamSpec;
  value: unknown;
  invalid?: boolean;
  templateCandidates?: TemplateCandidate[];
  onChange: (value: unknown) => void;
}) {
  return (
    <div className="schema-form__field">
      <label className="field__label" htmlFor={`param-${param.name}`}>
        {param.label}
        {param.required ? <span className="field__required" title="Required">*</span> : null}
      </label>

      <ParamControl
        param={param}
        value={value}
        invalid={invalid}
        templateCandidates={templateCandidates}
        onChange={onChange}
      />

      {param.description ? (
        <p className="field__hint">{param.description}</p>
      ) : null}
    </div>
  );
}

function ParamControl({
  param,
  value,
  invalid,
  templateCandidates,
  onChange,
}: {
  param: ParamSpec;
  value: unknown;
  invalid?: boolean;
  templateCandidates?: TemplateCandidate[];
  onChange: (value: unknown) => void;
}) {
  const t = useT();
  const id = `param-${param.name}`;
  const invalidClass = invalid ? ' input--invalid' : '';

  switch (param.type) {
    case 'string': {
      const text = typeof value === 'string' ? value : '';

      if (param.enum?.length) {
        return (
          <select
            id={id}
            className={`select${invalidClass}`}
            value={text}
            onChange={(event) => onChange(event.target.value)}
          >
            <option value="">—</option>
            {param.enum.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        );
      }

      if (param.multiline) {
        return (
          <TemplateTextarea
            id={id}
            value={text}
            rows={param.rows ?? 4}
            placeholder={param.placeholder ?? ''}
            invalidClass={invalidClass}
            candidates={templateCandidates ?? []}
            onChange={onChange}
          />
        );
      }

      return (
        <input
          id={id}
          type={param.secret ? 'password' : 'text'}
          className={`input${invalidClass}`}
          value={text}
          placeholder={param.placeholder ?? ''}
          onChange={(event) => onChange(event.target.value)}
          autoComplete={param.secret ? 'new-password' : 'off'}
        />
      );
    }

    case 'number': {
      const numeric = typeof value === 'number' ? value : '';
      return (
        <input
          id={id}
          type="number"
          className={`input${invalidClass}`}
          value={numeric}
          min={param.min ?? undefined}
          max={param.max ?? undefined}
          step={param.step ?? 1}
          onChange={(event) => {
            const raw = event.target.value;
            if (raw === '') {
              onChange(undefined);
              return;
            }
            const parsed = param.integer ? Number.parseInt(raw, 10) : Number.parseFloat(raw);
            onChange(Number.isNaN(parsed) ? undefined : parsed);
          }}
        />
      );
    }

    case 'boolean': {
      const checked = value === true;
      return (
        <label className="checkbox">
          <input
            id={id}
            type="checkbox"
            checked={checked}
            onChange={(event) => onChange(event.target.checked)}
          />
          <span className="muted">{checked ? t('schema.on') : t('schema.off')}</span>
        </label>
      );
    }

    case 'select': {
      const current = typeof value === 'string' ? value : '';
      return (
        <select
          id={id}
          className={`select${invalidClass}`}
          value={current}
          onChange={(event) => onChange(event.target.value)}
        >
          {param.options.map((option) => (
            <option key={option.value} value={option.value} title={option.description}>
              {option.label}
            </option>
          ))}
        </select>
      );
    }

    case 'json':
      return (
        <JsonField
          id={id}
          value={value}
          rows={param.rows ?? 6}
          invalid={invalid}
          templateCandidates={templateCandidates}
          onChange={onChange}
        />
      );

    case 'code':
      return (
        <textarea
          id={id}
          className={`textarea textarea--code${invalidClass}`}
          value={typeof value === 'string' ? value : param.default ?? ''}
          rows={param.rows ?? 12}
          spellCheck={false}
          onChange={(event) => onChange(event.target.value)}
        />
      );

    case 'model':
      return <ModelField id={id} value={value} onChange={onChange} />;

    default: {
      // Exhaustiveness guard: adding a param kind on the backend without
      // handling it here becomes a compile error, not a blank field.
      const exhaustive: never = param;
      return (
        <p className="field__hint">
          {t('schema.unsupported', { value: JSON.stringify(exhaustive) })}
        </p>
      );
    }
  }
}

// ---------------------------------------------------------------------------
// JSON field
// ---------------------------------------------------------------------------

function JsonField({
  id,
  value,
  rows,
  invalid,
  templateCandidates,
  onChange,
}: {
  id: string;
  value: unknown;
  rows: number;
  invalid?: boolean;
  templateCandidates?: TemplateCandidate[];
  onChange: (value: unknown) => void;
}) {
  const t = useT();
  const serialised = useMemo(() => formatJson(value), [value]);
  const [draft, setDraft] = useState(serialised);
  const [error, setError] = useState<string | null>(null);

  // Adopt external changes (an accepted AI suggestion, an undo) unless the user
  // is mid-edit with a draft that does not yet parse.
  useEffect(() => {
    setDraft((current) => (current === serialised ? current : serialised));
    setError(null);
  }, [serialised]);

  return (
    <div className="json-field">
      <TemplateTextarea
        id={id}
        value={draft}
        rows={rows}
        placeholder=""
        code
        spellCheck={false}
        invalidClass={error || invalid ? ' input--invalid' : ''}
        candidates={templateCandidates ?? []}
        onChange={(next) => {
          setDraft(next);

          if (next.trim() === '') {
            setError(null);
            onChange(undefined);
            return;
          }
          try {
            const parsed = JSON.parse(next);
            setError(null);
            onChange(parsed);
          } catch (parseError) {
            // Report but do not commit — the last valid value stays in the store.
            setError(
              t('common.jsonInvalidDetail', {
                detail: parseError instanceof Error ? parseError.message : t('schema.invalidJson'),
              }),
            );
          }
        }}
      />
      {error ? (
        <p className="json-field__error">
          <AlertCircle size={11} /> {error}
        </p>
      ) : null}
    </div>
  );
}

function formatJson(value: unknown): string {
  if (value === undefined || value === null) return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

// ---------------------------------------------------------------------------
// Model field
// ---------------------------------------------------------------------------

/**
 * Per-node model override.
 *
 * Left blank the node uses the global model from Settings, which is what most
 * people want; the field exists so a workflow can mix a cheap model for routing
 * with an expensive one for the final answer.
 */
function ModelField({
  id,
  value,
  onChange,
}: {
  id: string;
  value: unknown;
  onChange: (value: unknown) => void;
}) {
  const t = useT();
  const config = (value && typeof value === 'object' ? value : {}) as Record<string, unknown>;

  return (
    <div className="model-field">
      <input
        id={id}
        className="input"
        placeholder={t('schema.modelPlaceholder')}
        value={typeof config.model === 'string' ? config.model : ''}
        onChange={(event) => {
          const next = { ...config };
          if (event.target.value.trim()) next.model = event.target.value.trim();
          else delete next.model;
          onChange(Object.keys(next).length ? next : undefined);
        }}
      />
      <input
        className="input"
        placeholder={t('schema.baseUrlPlaceholder')}
        value={typeof config.base_url === 'string' ? config.base_url : ''}
        onChange={(event) => {
          const next = { ...config };
          if (event.target.value.trim()) next.base_url = event.target.value.trim();
          else delete next.base_url;
          onChange(Object.keys(next).length ? next : undefined);
        }}
      />
    </div>
  );
}
