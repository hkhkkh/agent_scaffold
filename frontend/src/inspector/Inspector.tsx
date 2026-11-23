/**
 * Inspector panel.
 *
 * Three jobs, in the order they matter:
 *
 *  1. **Let the user describe the node.** A prominent textarea for "what should
 *     this step do", which is both the canvas annotation and the AI's input.
 *  2. **Apply AI suggestions as a reviewable diff.** `Describe → Fill` never
 *     mutates the graph directly; it shows what would change and waits for
 *     acceptance. That is what makes the feature trustworthy.
 *  3. **Edit the typed configuration**, rendered generically from the backend's
 *     param specs.
 */

import { useCallback, useMemo, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  ArrowRight,
  Check,
  ChevronDown,
  Copy,
  Info,
  Loader2,
  Sparkles,
  Trash2,
  Wand2,
  Zap,
} from 'lucide-react';

import { ApiError, api } from '@/api/client';
import {
  assistKeysInPlace,
  assistNoteApplies,
  useAssistNote,
  type AssistNote,
} from '@/assist/auto';
import { useT } from '@/i18n';
import { SchemaForm } from '@/inspector/SchemaForm';
import { buildTemplateCandidates } from '@/inspector/templateCompletions';
import {
  selectSelectedNode,
  useWorkflow,
} from '@/store/workflowStore';
import type { NodeFillSuggestion, NodeTypeSpec, RunStep } from '@/types/workflow';
import { Icon } from '@/ui/Icon';
import { JsonViewer } from '@/ui/JsonViewer';

export function Inspector() {
  const t = useT();
  const node = useWorkflow(selectSelectedNode);
  const selectedEdgeId = useWorkflow((state) => state.selectedEdgeId);
  const nodes = useWorkflow((state) => state.nodes);
  const edges = useWorkflow((state) => state.edges);
  const nodeTypes = useWorkflow((state) => state.nodeTypes);
  const issues = useWorkflow((state) => state.issues);
  const aiStatus = useWorkflow((state) => state.aiStatus);
  const stepByNode = useWorkflow((state) => state.stepByNode);
  /** The selected node's step from the last run, when there was one. */
  const runStep = node ? stepByNode[node.id] : undefined;
  /**
   * What the assistant last did here on its own, if anything this session — and
   * only while it is still true. An undone fill must not leave the panel
   * claiming credit for settings that are no longer on the node.
   */
  const recordedNote = useAssistNote(node?.id ?? null);
  const nodeConfig = node?.data.spec.config ?? {};
  const assistNote = assistNoteApplies(recordedNote, nodeConfig) ? recordedNote : undefined;
  const assistKeys = assistNote ? assistKeysInPlace(assistNote, nodeConfig) : [];

  /**
   * Paths offered inside `{{ }}` fields. Recomputed when the graph changes, so
   * a node added or renamed a moment ago is already in the list. Outputs from
   * the last run are folded in because Code/Tool/Agent declare only `out` —
   * without them the menu can offer `vars.search.out` but not the useful
   * `vars.search.out.results`.
   */
  const templateCandidates = useMemo(() => {
    if (!node) return [];
    const observed: Record<string, Record<string, unknown> | null> = {};
    for (const [id, step] of Object.entries(stepByNode)) observed[id] = step.outputs;
    return buildTemplateCandidates({
      nodes,
      edges,
      nodeTypes,
      currentNodeId: node.id,
      observedOutputs: observed,
    });
  }, [node, nodes, edges, nodeTypes, stepByNode]);

  const updateNode = useWorkflow((state) => state.updateNode);
  const updateConfig = useWorkflow((state) => state.updateConfig);
  const removeNode = useWorkflow((state) => state.removeNode);
  const duplicateNode = useWorkflow((state) => state.duplicateNode);
  const removeEdge = useWorkflow((state) => state.removeEdge);
  const setEdgeLabel = useWorkflow((state) => state.setEdgeLabel);

  if (selectedEdgeId) {
    const edge = edges.find((item) => item.id === selectedEdgeId);
    if (edge) {
      return (
        <EdgeInspector
          edge={edge}
          onLabel={(label) => setEdgeLabel(edge.id, label)}
          onDelete={() => removeEdge(edge.id)}
        />
      );
    }
  }

  if (!node) {
    return (
      <div className="inspector inspector--empty">
        <p className="empty">
          {t('inspector.empty')}
          <br />
          <span className="dim">{t('inspector.emptyHint')}</span>
        </p>
      </div>
    );
  }

  const typeSpec = nodeTypes[node.data.spec.type];
  const nodeIssues = issues.filter((issue) => issue.node_id === node.id);

  return (
    <div className="inspector scroll">
      <header className="inspector__header">
        <span className="inspector__icon" style={{ color: node.data.color }}>
          <Icon name={node.data.icon} size={15} />
        </span>
        <span className="inspector__title">{typeSpec?.label ?? node.data.spec.type}</span>

        <div className="spacer" />

        <button
          type="button"
          className="btn btn--ghost btn--icon btn--sm"
          title={t('inspector.duplicate')}
          onClick={() => duplicateNode(node.id)}
        >
          <Copy size={12} />
        </button>
        <button
          type="button"
          className="btn btn--ghost btn--icon btn--sm"
          title={t('inspector.delete')}
          onClick={() => removeNode(node.id)}
        >
          <Trash2 size={12} />
        </button>
      </header>

      {typeSpec?.description ? (
        <p className="inspector__type-hint">{typeSpec.description}</p>
      ) : null}

      {assistNote ? (
        <NodeAssistSummary note={assistNote} keys={assistKeys} typeSpec={typeSpec} />
      ) : null}

      {runStep ? <NodeRunSummary step={runStep} /> : null}

      {nodeIssues.length > 0 ? (
        <div className="inspector__section">
          {nodeIssues.map((issue, index) => (
            <div
              key={`${issue.code}-${index}`}
              className={`callout callout--${issue.severity === 'error' ? 'error' : 'warning'}`}
            >
              <AlertCircle size={13} className="callout__icon" />
              <span>{issue.message}</span>
            </div>
          ))}
        </div>
      ) : null}

      {/* ---------------------------------------------------------------- */}
      {/* 1. Description — the field the whole product is built around.     */}
      {/* ---------------------------------------------------------------- */}

      <section className="inspector__section">
        <div className="section-label">{t('inspector.description')}</div>

        <textarea
          className="textarea"
          rows={4}
          value={node.data.spec.description}
          placeholder={t('inspector.descriptionPlaceholder')}
          onChange={(event) => updateNode(node.id, { description: event.target.value })}
        />

        <FillWithAI
          nodeId={node.id}
          description={node.data.spec.description}
          aiReady={aiStatus?.configured === true}
          aiReason={aiStatus?.reason ?? null}
        />
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* 2. Label                                                          */}
      {/* ---------------------------------------------------------------- */}

      <section className="inspector__section">
        <div className="section-label">{t('inspector.label')}</div>
        <input
          className="input"
          value={node.data.spec.label}
          onChange={(event) => updateNode(node.id, { label: event.target.value })}
        />
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* 3. Code trust                                                     */}
      {/* ---------------------------------------------------------------- */}

      {typeSpec?.requires_trust ? (
        <section className="inspector__section">
          <label className="checkbox inspector__trust">
            <input
              type="checkbox"
              checked={node.data.spec.trusted}
              onChange={(event) => updateNode(node.id, { trusted: event.target.checked })}
            />
            <span>
              <strong>{t('inspector.trusted')}</strong>
              <span className="field__hint">{t('inspector.trustedHint')}</span>
            </span>
          </label>
        </section>
      ) : null}

      {/* ---------------------------------------------------------------- */}
      {/* 4. Generated configuration form                                   */}
      {/* ---------------------------------------------------------------- */}

      <section className="inspector__section">
        <div className="section-label">{t('inspector.settings')}</div>
        {typeSpec ? (
          <SchemaForm
            params={typeSpec.params}
            config={node.data.spec.config}
            templateCandidates={templateCandidates}
            onChange={(name, value) => updateConfig(node.id, name, value)}
          />
        ) : (
          <div className="callout callout--warning">
            <AlertCircle size={13} className="callout__icon" />
            <span>{t('inspector.unregistered', { type: node.data.spec.type })}</span>
          </div>
        )}
      </section>

      {/* ---------------------------------------------------------------- */}
      {/* 5. Node test                                                      */}
      {/* ---------------------------------------------------------------- */}

      <NodeTestSection nodeId={node.id} />

      {/* ---------------------------------------------------------------- */}
      {/* 6. Ports reference                                                */}
      {/* ---------------------------------------------------------------- */}

      <section className="inspector__section">
        <details className="inspector__details">
          <summary>
            <ChevronDown size={12} /> {t('inspector.ports')}
          </summary>
          <div className="inspector__ports">
            <div>
              <div className="section-label">{t('inspector.inputs')}</div>
              {node.data.inputs.length === 0 ? (
                <p className="field__hint">{t('inspector.none')}</p>
              ) : (
                <ul className="inspector__port-list">
                  {node.data.inputs.map((port) => (
                    <li key={port.id}>
                      <span className="mono">{port.id}</span>
                      <span className="badge">{port.type}</span>
                      {port.required ? (
                        <span className="badge badge--warning">{t('inspector.required')}</span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <div className="section-label">{t('inspector.outputs')}</div>
              <ul className="inspector__port-list">
                {node.data.outputs.map((port) => (
                  <li key={port.id}>
                    <span className="mono">{port.id}</span>
                    <span className="badge">{port.type}</span>
                  </li>
                ))}
              </ul>
              <p className="field__hint">
                {t('inspector.referencePrefix')}{' '}
                <code className="mono">{`{{ vars.${node.id}.${node.data.outputs[0]?.id ?? 'out'} }}`}</code>
              </p>
            </div>
          </div>
        </details>
      </section>
    </div>
  );
}

// ---------------------------------------------------------------------------
// AI fill-in
// ---------------------------------------------------------------------------

function FillWithAI({
  nodeId,
  description,
  aiReady,
  aiReason,
}: {
  nodeId: string;
  description: string;
  aiReady: boolean;
  aiReason: string | null;
}) {
  const updateConfig = useWorkflow((state) => state.updateConfig);
  const setError = useWorkflow((state) => state.setError);
  const t = useT();

  const [busy, setBusy] = useState(false);
  const [instruction, setInstruction] = useState('');
  const [showInstruction, setShowInstruction] = useState(false);
  const [suggestion, setSuggestion] = useState<NodeFillSuggestion | null>(null);

  const run = useCallback(async () => {
    setBusy(true);
    setSuggestion(null);
    setError(null);
    try {
      const result = await api.fillNode({
        graph: useWorkflow.getState().toGraph(),
        node_id: nodeId,
        instruction,
      });
      setSuggestion(result);
    } catch (error) {
      setError(
        error instanceof ApiError ? error.message : t('ai.requestFailed'),
      );
    } finally {
      setBusy(false);
    }
  }, [instruction, nodeId, setError, t]);

  const accept = useCallback(() => {
    if (!suggestion) return;
    for (const [key, value] of Object.entries(suggestion.config)) {
      updateConfig(nodeId, key, value);
    }
    setSuggestion(null);
  }, [nodeId, suggestion, updateConfig]);

  const hasDescription = description.trim().length > 0;

  return (
    <div className="fill-ai">
      <div className="row">
        <button
          type="button"
          className="btn btn--primary grow"
          disabled={busy || !hasDescription}
          onClick={run}
          title={hasDescription ? t('ai.fillHint') : t('ai.fillNeedsDescription')}
        >
          {busy ? <Loader2 size={12} className="spin" /> : <Wand2 size={12} />}
          {busy ? t('ai.filling') : t('ai.fill')}
        </button>

        <button
          type="button"
          className="btn btn--icon"
          onClick={() => setShowInstruction((value) => !value)}
          title={t('ai.instruction')}
          aria-expanded={showInstruction}
        >
          <ChevronDown
            size={12}
            style={{ transform: showInstruction ? 'rotate(180deg)' : undefined }}
          />
        </button>
      </div>

      {showInstruction ? (
        <input
          className="input"
          placeholder={t('ai.instructionPlaceholder')}
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
        />
      ) : null}

      {!aiReady && aiReason ? (
        <div className="callout callout--info">
          <Info size={13} className="callout__icon" />
          <span>{aiReason}</span>
        </div>
      ) : null}

      {suggestion ? (
        <SuggestionDiff
          suggestion={suggestion}
          onAccept={accept}
          onReject={() => setSuggestion(null)}
        />
      ) : null}
    </div>
  );
}

function SuggestionDiff({
  suggestion,
  onAccept,
  onReject,
}: {
  suggestion: NodeFillSuggestion;
  onAccept: () => void;
  onReject: () => void;
}) {
  const t = useT();
  const changes = suggestion.changes ?? [];

  return (
    <div className="suggestion">
      <header className="suggestion__header">
        <Sparkles size={12} />
        <strong>{t('ai.suggested')}</strong>
      </header>

      {suggestion.rationale ? (
        <p className="suggestion__rationale">{suggestion.rationale}</p>
      ) : null}

      {changes.length === 0 ? (
        <p className="field__hint">{t('ai.noChanges')}</p>
      ) : (
        <ul className="suggestion__changes">
          {changes.map((change) => (
            <li key={change.field}>
              <div className="suggestion__field mono">{change.field}</div>
              {change.before !== undefined && change.before !== null ? (
                <div className="suggestion__before">
                  <RemoveGlyph /> {preview(change.before)}
                </div>
              ) : null}
              <div className="suggestion__after">
                <ArrowRight size={10} /> {preview(change.after)}
              </div>
            </li>
          ))}
        </ul>
      )}

      {suggestion.warnings?.length ? (
        <div className="suggestion__warnings">
          {suggestion.warnings.map((warning, index) => (
            <div key={index} className="callout callout--warning">
              <AlertCircle size={12} className="callout__icon" />
              <span>{warning}</span>
            </div>
          ))}
        </div>
      ) : null}

      <footer className="suggestion__footer">
        <button type="button" className="btn btn--primary grow" onClick={onAccept}>
          <Check size={12} /> {t('ai.accept')}
        </button>
        <button type="button" className="btn grow" onClick={onReject}>
          {t('ai.reject')}
        </button>
      </footer>
    </div>
  );
}

/**
 * What this step received and handed on, in words.
 *
 * The run panel shows the values themselves — right for diagnosing, unreadable
 * for anyone else. This is the same fact one level up: how many things came in
 * and what they are called, and what went out. A step that produced nothing is
 * said plainly rather than shown as `{}`, because that is exactly the case a
 * green tick hides and the one worth spending the space on.
 */
/**
 * What the assistant set here on its own, named the way the form names it.
 *
 * The config keys it wrote are ids — `prompt`, `system_prompt` — and listing
 * those at someone who does not write code is a worse answer than saying
 * nothing. The node type knows the label each one is shown under, in the
 * reader's language, so that is what gets shown.
 */
function NodeAssistSummary({
  note,
  keys,
  typeSpec,
}: {
  note: AssistNote;
  keys: string[];
  typeSpec: NodeTypeSpec | undefined;
}) {
  const t = useT();

  if (note.failed) {
    return (
      <section className="inspector__section inspector__assist inspector__assist--failed">
        <div className="section-label">
          <AlertTriangle size={11} /> {t('assist.failed')}
        </div>
        <p className="inspector__run-line">{t('assist.failedHint')}</p>
      </section>
    );
  }

  const named = keys.map(
    (key) => typeSpec?.params.find((param) => param.name === key)?.label ?? key,
  );

  return (
    <section className="inspector__section inspector__assist">
      <div className="section-label">
        <Sparkles size={11} /> {t('assist.filled')}
      </div>
      <p className="inspector__run-line">
        {t('assist.filledFields', { fields: named.join(t('list.separator')) })}
      </p>
    </section>
  );
}

function NodeRunSummary({ step }: { step: RunStep }) {
  const t = useT();
  const received = Object.keys(step.inputs ?? {});
  const produced = Object.keys(step.outputs ?? {});
  const onlyKey = produced.length === 1 ? produced[0]! : null;
  const textLength =
    onlyKey !== null && typeof step.outputs?.[onlyKey] === 'string'
      ? String(step.outputs[onlyKey]).length
      : null;
  const logs = step.logs ?? [];

  return (
    <section className="inspector__section inspector__run">
      <div className="section-label">{t('node.run.title')}</div>

      <p className="inspector__run-line">
        {received.length === 0
          ? t('node.run.receivedNone')
          : t('node.run.received', { names: received.join(' · ') })}
      </p>

      <p className="inspector__run-line">
        {produced.length === 0
          ? t('node.run.producedNone')
          : textLength !== null
            ? t('node.run.producedText', { n: String(textLength) })
            : t('node.run.producedFields', {
                n: String(produced.length),
                names: produced.join(' · '),
              })}
      </p>

      {logs.length > 0 ? (
        <details className="inspector__run-logs">
          <summary>{t('node.run.logs')}</summary>
          <ul>
            {logs.map((line, index) => (
              <li key={index}>{line}</li>
            ))}
          </ul>
        </details>
      ) : null}
    </section>
  );
}

function RemoveGlyph() {
  return <span className="suggestion__remove">−</span>;
}

function preview(value: unknown): string {
  if (value === null || value === undefined) return '—';
  if (typeof value === 'string') {
    return value.length > 220 ? `${value.slice(0, 220)}…` : value;
  }
  try {
    const text = JSON.stringify(value);
    return text.length > 220 ? `${text.slice(0, 220)}…` : text;
  } catch {
    return String(value);
  }
}

// ---------------------------------------------------------------------------
// Node test
// ---------------------------------------------------------------------------

/**
 * Run one node in isolation with hand-supplied input.
 *
 * This is the highest-leverage debugging affordance in the app: fixing a prompt
 * means running *that node*, not the whole workflow and its dependencies.
 */
function NodeTestSection({ nodeId }: { nodeId: string }) {
  const t = useT();
  const node = useWorkflow(selectSelectedNode);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [inputs, setInputs] = useState('{}');
  const [result, setResult] = useState<
    | { ok: boolean; outputs?: Record<string, unknown>; logs: { message: string; level: string }[]; error?: string }
    | null
  >(null);
  const [parseError, setParseError] = useState<string | null>(null);

  const handleInputsChange = (text: string) => {
    setInputs(text);
    try {
      JSON.parse(text || '{}');
      setParseError(null);
    } catch (error) {
      setParseError(
        t('common.jsonInvalidDetail', {
          detail: error instanceof Error ? error.message : '',
        }),
      );
    }
  };

  const run = useCallback(async () => {
    setBusy(true);
    setResult(null);
    try {
      const current = useWorkflow.getState().nodes.find((item) => item.id === nodeId);
      const response = await api.testNode({
        workflow_id: useWorkflow.getState().graphId,
        node_id: nodeId,
        // The node under the cursor, not the last saved copy. Without it, a node
        // added but not yet saved answered `No node '<id>'` with HTTP 404, and a
        // config edit that had not been saved was not the config under test.
        node: current ? { ...current.data.spec, position: current.position } : undefined,
        inputs: JSON.parse(inputs || '{}'),
      });
      setResult(response);
    } catch (error) {
      setResult({
        ok: false,
        logs: [],
        error: error instanceof Error ? error.message : t('test.testFailed'),
      });
    } finally {
      setBusy(false);
    }
  }, [inputs, nodeId, t]);

  // Pre-fill with the node's own input handle names, so the common case is
  // "type a value" rather than "remember the handle ids".
  const inputTemplate = useMemo(() => {
    const template: Record<string, unknown> = {};
    for (const port of node?.data.inputs ?? []) template[port.id] = '';
    return JSON.stringify(template, null, 2);
  }, [node]);

  return (
    <section className="inspector__section">
      <button
        type="button"
        className="inspector__disclosure"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <ChevronDown
          size={12}
          style={{ transform: open ? undefined : 'rotate(-90deg)' }}
        />
        {t('test.title')}
      </button>

      {open ? (
        <div className="node-test">
          <p className="field__hint">{t('test.hint')}</p>

          <textarea
            className={`textarea textarea--code${parseError ? ' input--invalid' : ''}`}
            rows={5}
            value={inputs === '{}' ? inputTemplate : inputs}
            onChange={(event) => handleInputsChange(event.target.value)}
            spellCheck={false}
            aria-label={t('test.inputs')}
          />
          {parseError ? <p className="json-field__error">{parseError}</p> : null}

          <button
            type="button"
            className="btn grow"
            onClick={run}
            disabled={busy || !!parseError}
          >
            {busy ? <Loader2 size={12} className="spin" /> : <Zap size={12} />}
            {busy ? t('test.running') : t('test.run')}
          </button>

          {result ? (
            <div className="node-test__result">
              {result.ok ? (
                <span className="badge badge--success">{t('test.succeeded')}</span>
              ) : (
                <span className="badge badge--danger">{t('test.failed')}</span>
              )}

              {result.error ? (
                <div className="callout callout--error">
                  <AlertCircle size={12} className="callout__icon" />
                  <span>{result.error}</span>
                </div>
              ) : null}

              {result.logs?.length ? (
                <ul className="node-test__logs">
                  {result.logs.map((entry, index) => (
                    <li key={index} className={`node-test__log node-test__log--${entry.level}`}>
                      {entry.message}
                    </li>
                  ))}
                </ul>
              ) : null}

              {result.outputs ? (
                <JsonViewer value={result.outputs} label={t('test.output')} />
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Edge inspector
// ---------------------------------------------------------------------------

function EdgeInspector({
  edge,
  onLabel,
  onDelete,
}: {
  edge: { id: string; source: string; target: string; sourceHandle?: string | null; targetHandle?: string | null; label?: unknown };
  onLabel: (label: string) => void;
  onDelete: () => void;
}) {
  const nodes = useWorkflow((state) => state.nodes);
  const t = useT();
  const source = nodes.find((node) => node.id === edge.source);
  const target = nodes.find((node) => node.id === edge.target);

  return (
    <div className="inspector scroll">
      <header className="inspector__header">
        <span className="inspector__title">{t('inspector.edge.title')}</span>
        <div className="spacer" />
        <button
          type="button"
          className="btn btn--ghost btn--icon btn--sm"
          onClick={onDelete}
          title={t('inspector.edge.delete')}
        >
          <Trash2 size={12} />
        </button>
      </header>

      <section className="inspector__section">
        <div className="edge-summary">
          <span className="edge-summary__node">{source?.data.spec.label ?? edge.source}</span>
          <ArrowRight size={12} />
          <span className="edge-summary__node">{target?.data.spec.label ?? edge.target}</span>
        </div>
        <p className="field__hint mono">
          {edge.source}.{edge.sourceHandle ?? 'out'} → {edge.target}.
          {edge.targetHandle ?? 'in'}
        </p>
      </section>

      <section className="inspector__section">
        <div className="section-label">{t('inspector.edge.label')}</div>
        <input
          className="input"
          value={typeof edge.label === 'string' ? edge.label : ''}
          placeholder={t('inspector.edge.placeholder')}
          onChange={(event) => onLabel(event.target.value)}
        />
      </section>
    </div>
  );
}
