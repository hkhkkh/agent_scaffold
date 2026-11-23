/**
 * TypeScript mirror of the backend's graph schema.
 *
 * Source of truth: `backend/app/graph/schema.py`.
 *
 * **Nothing checks this file for drift.** It is hand-maintained, and this header
 * used to claim otherwise: it told you to run `scripts/check-schema-drift.ps1`,
 * which has never existed. The cost of that gap is the quiet kind — a field added
 * on the backend and forgotten here renders as an Inspector control that simply
 * never appears, and tracing that back to a missing line in this file is slow.
 *
 * The spec for the missing checker is `docs/code/task-13-postgres.md` §13b; it is
 * also listed in `docs/current/limitations.md` §3.1. Two rules it has to settle
 * before it can be useful, both measured rather than assumed:
 *
 * 1. **Ignore `*_zh`.** `schema.py` declares `label_zh` / `description_zh` /
 *    `message_zh` on nearly every model, but `localize()` in `app/api/catalogue.py`
 *    resolves the language server-side and strips them before the payload is sent.
 *    Comparing them here would report a mismatch on almost every interface and
 *    drown the one real difference.
 * 2. **Ignore optionality.** A backend field with a default is absent from
 *    `model_json_schema()["required"]` yet is always serialized, so comparing
 *    "required" against this file's `?` marks nearly every field as different.
 *    What the two sides disagree about is fields that are *missing entirely*.
 */

// ---------------------------------------------------------------------------
// Ports
// ---------------------------------------------------------------------------

export const PORT_TYPES = [
  'any',
  'text',
  'message',
  'messages',
  'language_model',
  'tool',
  'retriever',
  'memory',
  'json',
  'table',
  'embeddings',
  'number',
  'boolean',
] as const;

export type PortType = (typeof PORT_TYPES)[number];

/** Fallback colours. The backend sends the authoritative map at /api/node-types. */
export const DEFAULT_PORT_COLORS: Record<PortType, string> = {
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

export interface InputPort {
  id: string;
  label: string;
  type: PortType;
  required?: boolean;
  description?: string;
}

export interface OutputPort {
  id: string;
  label: string;
  type: PortType;
  description?: string;
}

/**
 * Which port types may connect to which.
 *
 * Mirrors `_PORT_COMPATIBILITY` in `schema.py`. The backend is still the
 * authority (it re-validates on save), but checking here gives instant feedback
 * while dragging instead of an error after the fact.
 */
export const PORT_COMPATIBILITY: Record<PortType, readonly PortType[]> = {
  any: PORT_TYPES,
  text: ['any', 'text', 'message'],
  message: ['any', 'message', 'text', 'messages'],
  messages: ['any', 'messages', 'message'],
  language_model: ['any', 'language_model'],
  tool: ['any', 'tool'],
  retriever: ['any', 'retriever'],
  memory: ['any', 'memory'],
  json: ['any', 'json', 'table'],
  table: ['any', 'table', 'json'],
  embeddings: ['any', 'embeddings'],
  number: ['any', 'number', 'text'],
  boolean: ['any', 'boolean', 'text'],
};

export function portsCompatible(source: PortType, target: PortType): boolean {
  if (source === 'any' || target === 'any') return true;
  return PORT_COMPATIBILITY[source].includes(target);
}

// ---------------------------------------------------------------------------
// Parameters — the discriminated union that drives the Inspector form
// ---------------------------------------------------------------------------

export interface ParamBase {
  name: string;
  label: string;
  description?: string;
  required?: boolean;
  advanced?: boolean;
  /** Show this field only when another config key equals one of these values. */
  show_if?: { field: string; equals: unknown[] } | null;
}

export interface StringParam extends ParamBase {
  type: 'string';
  default?: string;
  placeholder?: string;
  multiline?: boolean;
  rows?: number;
  secret?: boolean;
  enum?: string[] | null;
}

export interface NumberParam extends ParamBase {
  type: 'number';
  default?: number;
  min?: number | null;
  max?: number | null;
  step?: number;
  integer?: boolean;
}

export interface BooleanParam extends ParamBase {
  type: 'boolean';
  default?: boolean;
}

export interface SelectOption {
  label: string;
  value: string;
  description?: string;
}

export interface SelectParam extends ParamBase {
  type: 'select';
  default?: string;
  options: SelectOption[];
}

export interface JsonParam extends ParamBase {
  type: 'json';
  default?: unknown;
  rows?: number;
}

export interface CodeParam extends ParamBase {
  type: 'code';
  language?: 'python' | 'javascript' | 'jinja2';
  default?: string;
  rows?: number;
}

export interface ModelParam extends ParamBase {
  type: 'model';
  default?: Record<string, unknown>;
}

/**
 * Discriminated on `type`. The `never` branch makes an unhandled variant a
 * compile error rather than a silently blank field.
 */
export type ParamSpec =
  | StringParam
  | NumberParam
  | BooleanParam
  | SelectParam
  | JsonParam
  | CodeParam
  | ModelParam;

// ---------------------------------------------------------------------------
// Node types
// ---------------------------------------------------------------------------

export const NODE_CATEGORIES = [
  'input',
  'output',
  'model',
  'logic',
  'data',
  'integration',
  'control',
  'human',
  'custom',
] as const;

export type NodeCategory = (typeof NODE_CATEGORIES)[number];

export const CATEGORY_LABELS: Record<NodeCategory, string> = {
  input: 'Input',
  output: 'Output',
  model: 'Models',
  logic: 'Logic',
  data: 'Data',
  integration: 'Integrations',
  control: 'Control flow',
  human: 'Human in the loop',
  custom: 'Custom',
};

export type DynamicOutputs = 'none' | 'from_param';

export interface NodeTypeSpec {
  type: string;
  label: string;
  category: NodeCategory;
  description: string;
  icon: string;
  color: string;
  inputs: InputPort[];
  outputs: OutputPort[];
  params: ParamSpec[];
  dynamic_outputs: DynamicOutputs;
  dynamic_outputs_param: string | null;
  is_entry: boolean;
  is_terminal: boolean;
  is_container: boolean;
  supports_streaming: boolean;
  /** Runs user code: the node instance must be marked `trusted` before it will run. */
  requires_trust: boolean;
  /** Disabled in this deployment (the public demo turns Code off). */
  disabled?: boolean;
  disabled_reason?: string;
  /**
   * Runtime behaviour the *authoring model* is told about this type — where a
   * Loop gate belongs, that the Code sandbox has no file access, that the
   * Retriever is keyword search rather than vector search.
   *
   * Present in the payload because the catalogue endpoint serialises the whole
   * spec, but the UI never renders it: it is prompt material, written for a
   * model, and showing it in the Inspector would put a paragraph of English
   * implementation notes in front of a user who wanted to know what the node
   * does. Mirroring the field keeps this file an honest description of what the
   * backend actually sends.
   */
  ai_notes?: string;
}

// ---------------------------------------------------------------------------
// Graph instance
// ---------------------------------------------------------------------------

export interface Position {
  x: number;
  y: number;
}

export interface NodeUI {
  parent_id?: string | null;
  width?: number | null;
  height?: number | null;
  collapsed?: boolean;
  note?: string;
}

export interface NodeSpec {
  id: string;
  type: string;
  label: string;
  /**
   * The user's own words describing what this step should do.
   *
   * Rendered on the node card and sent to the AI when filling in config — this
   * is the field the whole "describe it, let the AI implement it" workflow is
   * built around.
   */
  description: string;
  position: Position;
  config: Record<string, unknown>;
  ui: NodeUI;
  /** Required for node types with `requires_trust`. */
  trusted: boolean;
}

export interface EdgeSpec {
  id: string;
  source: string;
  /** camelCase on the wire: React Flow's own field names. */
  sourceHandle: string | null;
  target: string;
  targetHandle: string | null;
  label?: string;
  type?: string;
}

export interface Viewport {
  x: number;
  y: number;
  zoom: number;
}

export type StateFieldType = 'text' | 'number' | 'boolean' | 'json' | 'list' | 'messages';
export type ReducerType = 'overwrite' | 'append' | 'add_messages' | 'merge_dict';

export interface StateField {
  name: string;
  type: StateFieldType;
  reducer: ReducerType;
  default?: unknown;
  description?: string;
}

export interface WorkflowGraph {
  id: string;
  name: string;
  description: string;
  version: number;
  nodes: NodeSpec[];
  edges: EdgeSpec[];
  viewport: Viewport;
  state_schema: StateField[];
  created_at?: string;
  updated_at?: string;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

export type ValidationSeverity = 'error' | 'warning';

export interface ValidationIssue {
  severity: ValidationSeverity;
  code: string;
  message: string;
  node_id?: string | null;
  edge_id?: string | null;
}

export interface ValidationReport {
  ok: boolean;
  issues: ValidationIssue[];
}

// ---------------------------------------------------------------------------
// Runs
// ---------------------------------------------------------------------------

export type RunStatus =
  | 'pending'
  /** Accepted, waiting for a free execution slot. See `runtime/queue.py`. */
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'interrupted'
  | 'cancelled';

export type RunEventType =
  | 'run_started'
  /** The run is waiting for a free slot; carries its position in the queue. */
  | 'run_queued'
  | 'node_started'
  | 'node_finished'
  | 'node_failed'
  | 'node_output_chunk'
  | 'state_updated'
  | 'interrupt_raised'
  | 'run_finished'
  | 'run_failed'
  | 'log';

export interface RunEvent {
  type: RunEventType;
  run_id: string;
  seq: number;
  node_id: string | null;
  node_label: string | null;
  step: number | null;
  timestamp: string;
  data: Record<string, unknown>;
}

export interface RunStep {
  node_id: string;
  node_label: string;
  index: number;
  status: 'running' | 'succeeded' | 'failed' | 'skipped';
  inputs: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  error: string | null;
  duration_ms: number | null;
  /**
   * Lines the node logged, already redacted.
   *
   * On the wire since the log event was wired up — `_step_to_payload` returns
   * them — but missing from this type, which is why nothing rendered them.
   */
  logs?: string[];
}

export interface InterruptPayload {
  message?: string;
  fields?: Record<string, { type?: string; label?: string; description?: string }>;
  allow_reject?: boolean;
  context?: unknown;
  __interrupt_id?: string;
  value?: unknown;
}

/**
 * The run's answer, and the key it arrived under.
 *
 * An End node returns `{"<output_label>": value}`, so the name of the result is
 * already a key on `outputs` — this says which one to read, sparing the UI from
 * guessing (`RunPanel` used to hardcode a key list, and showed nothing for any
 * workflow whose label was not in it).
 *
 * `null` on a summary means the payload did not unambiguously contain one
 * answer: a run stopped before reaching its End node, or several candidates.
 */
export interface FinalOutput {
  key: string;
  value: unknown;
}

export interface RunSummary {
  id: string;
  workflow_id: string;
  status: RunStatus;
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown>;
  final_output?: FinalOutput | null;
  error: string | null;
  interrupt?: InterruptPayload | null;
  created_at: string;
  finished_at: string | null;
  duration_ms: number | null;
  /**
   * What the run spent, and the limits it was judged against.
   *
   * `null` for a run read back from the database: spend is not persisted, so a
   * zeroed figure would claim the run used nothing.
   */
  usage?: RunUsage | null;
  last_seq: number;
  steps?: RunStep[];
}

/** Token counts for one authoring/model reply, as the provider reported them. */
export interface TokenUsage {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  calls: number;
}

export interface RunUsage {
  tokens: number;
  /**
   * False when a provider never reported usage.
   *
   * Kept separate from the count: unreported is not the same as none, and the UI
   * must be able to say "not reported" rather than showing 0.
   */
  tokens_known: boolean;
  steps: number;
  elapsed_seconds: number;
  cost?: number;
  max_tokens?: number;
  max_cost?: number;
  max_wall_seconds?: number;
  max_steps?: number;
}

// ---------------------------------------------------------------------------
// Catalogue responses
// ---------------------------------------------------------------------------

export interface NodeTypesResponse {
  count: number;
  types: NodeTypeSpec[];
  port_colors: Record<string, string>;
}

export interface ToolSummary {
  name: string;
  label: string;
  description: string;
  parameters: Record<string, unknown>;
  dangerous: boolean;
}

export interface StateFieldOptions {
  types: { value: StateFieldType; label: string }[];
  reducers: { value: ReducerType; label: string; description: string }[];
  ports: { value: PortType; color: string }[];
}

export interface AIStatus {
  enabled: boolean;
  configured: boolean;
  mock: boolean;
  model: string;
  base_url: string;
  reason: string | null;
}

// ---------------------------------------------------------------------------
// AI request/response shapes
// ---------------------------------------------------------------------------

export interface NodeFillSuggestion {
  config: Record<string, unknown>;
  rationale: string;
  warnings: string[];
  changes: { field: string; before: unknown; after: unknown; kind: string }[];
}

/** How far a Loop node has got in the current (or last) run. */
export interface LoopProgress {
  /** 1-based gate evaluation count, as reported by the Loop node. */
  iteration: number;
  /** The configured `max_iterations` ceiling, when known. */
  max?: number;
  /** Whether the last gate evaluation chose `continue` over `done`. */
  continuing?: boolean;
}

export interface TrialRunEvent {
  kind: 'node_started' | 'node_finished' | 'node_failed' | 'log';
  node_id: string;
  node: string;
  at_ms?: number;
  duration_ms?: number;
  inputs?: Record<string, unknown> | null;
  outputs?: Record<string, unknown> | null;
  error?: string;
  message?: string;
  level?: string;
}

export interface TrialRunResult {
  status: 'passed' | 'failed' | 'skipped' | 'paused';
  ok: boolean;
  summary: string;
  error?: string | null;
  failed_node?: string | null;
  failed_node_label?: string | null;
  duration_ms?: number;
  /** The failure looks like network/auth trouble rather than a graph defect. */
  environment?: boolean;
  outputs?: Record<string, unknown>;
  /**
   * The bounded per-node timeline. Optional because older backends do not send
   * it; the Run tab then shows the summary only.
   */
  trace?: TrialRunEvent[];
}

export interface GraphGenerationResult {
  graph: WorkflowGraph;
  validation: ValidationReport;
  transcript: { tool: string; arguments: Record<string, unknown>; result: string }[];
  finished: boolean;
  summary: string;
  /**
   * How many times the model was handed the validator's complaints and asked to
   * fix them. `0` means the first draft passed.
   *
   * Optional because a backend older than this field does not send it, and the
   * dialog treats "absent" as "no corrections" rather than crashing on it.
   */
  corrections?: number;
  /** The trial run of the draft, or null when one did not happen. */
  test?: TrialRunResult | null;
  /** Runtime failures handed back to the model. */
  test_corrections?: number;
  /** Whether the server attempted a trial run at all. */
  self_test?: boolean;
  /** Tokens spent building this draft. */
  usage?: TokenUsage;
}

/**
 * One frame of the generation SSE stream.
 *
 * `graph` events are complete snapshots: the dialog draws the snapshot and
 * never replays an edit itself, so ids and layout have one source of truth.
 */
export interface GenerationEvent {
  type:
    | 'status'
    | 'tool'
    | 'tool_result'
    | 'graph'
    | 'validation'
    | 'self_test'
    | 'correcting'
    | 'usage'
    | 'done'
    | 'error';
  tool?: string;
  arguments?: Record<string, unknown>;
  ok?: boolean;
  result?: unknown;
  graph?: WorkflowGraph;
  changed?: {
    added_nodes: string[];
    removed_nodes: string[];
    updated_nodes: string[];
    added_edges: string[];
    removed_edges: string[];
    updated_edges: string[];
  };
  phase?: string;
  message?: string;
  code?: string;
  usage?: TokenUsage;
  total?: TokenUsage;
  errors?: { code: string; node_id?: string; message: string }[];
  corrections?: number;
  kind?: string;
  failed_node?: string | null;
}

// ---------------------------------------------------------------------------
// Editing copilot
// ---------------------------------------------------------------------------

export type CopilotMode = 'auto' | 'confirm';

export interface CopilotAssumption {
  text: string;
  kind: 'inferred' | 'assumed';
}

export interface CopilotSource {
  title?: string;
  url?: string;
  snippet?: string;
  engine?: string;
}

export interface CopilotBrief {
  goal: string;
  direction: string;
  /** A question is answered instead of turned into a plan. */
  request_kind?: 'edit' | 'question' | 'chat';
  answer?: string;
  /** Why the assistant chose this answer or direction (collapsible trace). */
  reasoning?: string;
  success_criteria: string[];
  explicit_requirements: string[];
  assumptions: CopilotAssumption[];
  confidence: 'high' | 'medium' | 'low';
  needs_research: boolean;
  /** The pre-plan read-only tool turn runs when this is true. */
  needs_investigation?: boolean;
  search_queries: string[];
  open_questions: string[];
  sources?: CopilotSource[];
}

export interface CopilotChange {
  action: string;
  target: string;
  why: string;
}

export interface CopilotPlan {
  diagnosis: string;
  focus_nodes: string[];
  changes: CopilotChange[];
  steps: string[];
  scope_expansion_needed: boolean;
  expected_result: string;
  risks: string[];
}

export interface CopilotResearch {
  status: string;
  queries: string[];
  sources: CopilotSource[];
  error?: string | null;
}

export interface CopilotOutputReview {
  matches: boolean | null;
  reason: string;
  gaps: string[];
  suggested_fix: string;
}

export interface CopilotResult {
  graph: WorkflowGraph;
  validation: ValidationReport;
  brief: CopilotBrief;
  plan: CopilotPlan;
  research: CopilotResearch;
  changes: {
    added_nodes: string[];
    removed_nodes: string[];
    updated_nodes: string[];
    added_edges: string[];
    removed_edges: string[];
    updated_edges: string[];
  };
  transcript: { tool: string; arguments: Record<string, unknown>; result: string }[];
  summary: string;
  test?: TrialRunResult | null;
  output_review?: CopilotOutputReview | null;
  applied: boolean;
  needs_approval: boolean;
  rollback: boolean;
  mode: CopilotMode;
  corrections: number;
  test_corrections: number;
  self_test: boolean;
  /** Tokens spent on this turn. */
  usage?: TokenUsage;
}

export interface CopilotApproval {
  brief: CopilotBrief;
  plan: CopilotPlan;
  research?: CopilotResearch;
}

export interface WorkflowVersionSummary {
  version: number;
  definition_hash: string;
  name: string;
  saved_by: string;
  created_at: string;
  node_count: number;
  edge_count: number;
  meta: {
    ai_change?: CopilotChangeRecord;
    [key: string]: unknown;
  };
}

/** The record attached to the next saved workflow version. */
export interface CopilotChangeRecord {
  request: string;
  mode: CopilotMode;
  brief: CopilotBrief;
  plan: CopilotPlan;
  summary: string;
  changes: CopilotResult['changes'];
  test?: TrialRunResult | null;
  output_review?: CopilotOutputReview | null;
  applied: boolean;
  rollback: boolean;
  at: string;
}

/**
 * One frame of the copilot SSE stream.
 *
 * `stage` events are the layered indicator above the canvas. `graph` events are
 * complete snapshots, so the canvas never replays an edit itself.
 */
export interface CopilotEvent {
  type:
    | 'stage'
    | 'thinking'
    | 'token'
    | 'brief'
    | 'plan'
    | 'approval'
    | 'graph'
    | 'tool'
    | 'tool_result'
    | 'validation'
    | 'self_test'
    | 'correcting'
    | 'output_review'
    | 'usage'
    | 'done'
    | 'error';
  stage?: string;
  status?: string;
  detail?: string | null;
  text?: string;
  /** Extend the previous reasoning block instead of starting a new one. */
  append?: boolean;
  brief?: CopilotBrief;
  plan?: CopilotPlan;
  research?: CopilotResearch;
  graph?: WorkflowGraph;
  changed?: CopilotResult['changes'];
  tool?: string;
  arguments?: Record<string, unknown>;
  ok?: boolean;
  result?: unknown;
  review?: CopilotOutputReview;
  phase?: string;
  message?: string;
  code?: string;
  usage?: TokenUsage;
  total?: TokenUsage;
  errors?: { code: string; node_id?: string; message: string }[];
  corrections?: number;
  kind?: string;
  failed_node?: string | null;
}

/** One entry in the AI's repair diff. `fields` names what changed on a node. */
export interface RepairDiffEntry {
  node_id: string;
  type: string;
  label: string;
}

export interface RepairDiff {
  nodes_added: RepairDiffEntry[];
  nodes_removed: RepairDiffEntry[];
  nodes_updated: { node_id: string; label: string; fields: string[] }[];
  edges_added: { source: string; target: string; target_handle?: string | null }[];
  edges_removed: { source: string; target: string; target_handle?: string | null }[];
  node_delta: number;
  edge_delta: number;
}

export interface GraphRepairResult {
  graph: WorkflowGraph;
  validation: ValidationReport;
  diff: RepairDiff;
  transcript: { tool: string; arguments: Record<string, unknown>; result: string }[];
  /** True when no blocking problem is left. */
  resolved: boolean;
  /** False when the AI produced no edit at all. */
  changed: boolean;
  /** Model round-trips used. Zero means the graph was already valid. */
  attempts: number;
  /** True when the loop gave up because edits stopped helping. */
  stalled: boolean;
  summary: string;
  warnings: string[];
  /**
   * Still broken *and* smaller than before — the shape of a repair that
   * "succeeded" by deleting the user's work. The UI must make this loud.
   */
  regressed: boolean;
  remaining: number;
}

export interface NodeToCodeResult {
  code: string;
  notes: string;
  /** True when the conversion cannot reproduce the original behaviour exactly. */
  lossy: boolean;
}

export interface CodeToNodeResult {
  config: Record<string, unknown>;
  rationale: string;
  warnings: string[];
  /** False when the target node type cannot express the code. */
  equivalent: boolean;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function defaultConfig(spec: NodeTypeSpec): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const param of spec.params) {
    if (param.default !== undefined && param.default !== null) {
      config[param.name] = structuredClone(param.default);
    }
  }
  return config;
}

export function createNode(
  spec: NodeTypeSpec,
  position: Position,
  id?: string,
): NodeSpec {
  return {
    id: id ?? `${spec.type}_${Math.random().toString(36).slice(2, 8)}`,
    type: spec.type,
    label: spec.label,
    description: '',
    position,
    config: defaultConfig(spec),
    ui: {},
    // Code-like nodes would be blocked by the validator; trusting them on
    // creation matches the intent of having dragged them in.
    trusted: false,
  };
}

export function emptyGraph(): WorkflowGraph {
  return {
    id: Math.random().toString(36).slice(2, 14),
    name: 'Untitled workflow',
    description: '',
    version: 1,
    nodes: [],
    edges: [],
    viewport: { x: 0, y: 0, zoom: 1 },
    state_schema: [
      {
        name: 'vars',
        type: 'json',
        reducer: 'merge_dict',
        default: {},
        description: 'Node outputs, keyed by node id.',
      },
      {
        name: 'messages',
        type: 'messages',
        reducer: 'add_messages',
        default: [],
        description: 'Conversation history, when used.',
      },
    ],
  };
}
