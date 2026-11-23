/**
 * Editor state.
 *
 * A single Zustand store holds the whole editing session: the graph, selection,
 * undo history, and run state. One store rather than several because these are
 * genuinely coupled — an undo must restore nodes, edges, *and* selection
 * together, and a run must be able to lock the canvas.
 *
 * Undo uses whole-graph snapshots. An op-log would use less memory, but at the
 * scale of a hand-drawn workflow (tens of nodes) the snapshot is a few kilobytes
 * and its semantics are trivially correct: there is no way for undo to leave the
 * graph in a state the user never created.
 */

import { create } from 'zustand';
import {
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type XYPosition,
} from '@xyflow/react';

import { api } from '@/api/client';
// Type-only, so this does not make the store depend on the assistant at
// runtime: `assist/auto` imports the store, and the direction has to stay one
// way or the two become a cycle.
import type { AssistNote } from '@/assist/auto';
import { translate } from '@/i18n';
import {
  createNode,
  emptyGraph,
  portsCompatible,
  type AIStatus,
  type CopilotChangeRecord,
  type EdgeSpec,
  type LoopProgress,
  type NodeSpec,
  type NodeTypeSpec,
  type PortType,
  type RunEvent,
  type RunStep,
  type RunSummary,
  type RunUsage,
  type StateField,
  type TrialRunResult,
  type ValidationIssue,
  type WorkflowGraph,
} from '@/types/workflow';

/**
 * A node's status for the current run.
 *
 * `skipped` is a real outcome — a branch the run did not take — and `pending`
 * is a node a live run has not reached yet. Both belong in the vocabulary: with
 * them missing, a skipped node, an un-run node and a node with no run at all
 * were three different situations drawn the same way.
 */
export type NodeRunStatus = 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped';

/**
 * How much a node produced, in the only terms a card has room for.
 *
 * `empty` is the state worth naming out loud: a step that ran, reported success
 * and passed nothing on is exactly the failure a green tick hides. An edge
 * cannot show this — edges express order, not data — so it belongs on the card.
 */
export interface NodeOutputSummary {
  kind: 'text' | 'fields' | 'empty';
  /** Characters for `text`, field count for `fields`, 0 for `empty`. */
  size: number;
}

/** React Flow's node data, carrying our domain node through. */
export interface CanvasNodeData extends Record<string, unknown> {
  spec: NodeSpec;
  /** Resolved output ports, which may be dynamic (a Condition node's branches). */
  outputs: { id: string; label: string; type: PortType }[];
  inputs: { id: string; label: string; type: PortType; required?: boolean }[];
  /** Presentation, taken from the backend catalogue so it stays in sync. */
  color: string;
  icon: string;
  /** Execution status, when a run is in progress or has finished. */
  runStatus?: NodeRunStatus;
  /**
   * Warning-level lines this node logged during the run.
   *
   * Carried on the node, not only in the run panel, because a step that
   * succeeded *and* complained is the one that needs looking at — and a green
   * check mark is exactly what stops anyone from looking.
   */
  runWarnings?: string[];
  /**
   * What the assistant last did to this node, when it acted on its own.
   *
   * Derived like `runStatus` and for the same reason: it describes something
   * that happened rather than something the document says, so it must not be
   * part of the saved graph and must not mark the workflow dirty.
   */
  assistNote?: AssistNote;
  /** What this node handed on, once it finished. */
  runOutput?: NodeOutputSummary;
  /** Streaming text for the LLM/Agent nodes. */
  streamText?: string;
  /**
   * Which pass a Loop node is on, derived from the run's events.
   *
   * Kept on the node because "we are in round 4 of 7" is the one fact that makes
   * a loop legible on the canvas. Without it a cycle looks like a static
   * diagram while the run silently repeats part of it.
   */
  loopProgress?: LoopProgress;
  /**
   * How many validation *errors* point at this node.
   *
   * Derived on the canvas, like `runStatus`: it describes the current check
   * rather than the document, so it must never be saved.
   */
  errorCount?: number;
}

export type CanvasNode = Node<CanvasNodeData, string>;

const HISTORY_LIMIT = 50;

/**
 * Pending debounce for graph validation. Editing a field fires a change per
 * keystroke; validating each one floods the backend and makes the header
 * flicker between "checking" states, so edits validate once the typing pauses.
 */
let validateTimer: ReturnType<typeof setTimeout> | null = null;

/**
 * Whether a node drag is currently in flight, so exactly one undo entry is
 * pushed per drag (position changes arrive continuously while the mouse moves).
 */
let positionDrag: { active: boolean; at: number } = { active: false, at: 0 };

interface Snapshot {
  nodes: CanvasNode[];
  edges: Edge[];
  graphId: string;
  name: string;
  description: string;
}

export interface WorkflowState {
  // ---- document -----------------------------------------------------------
  graphId: string;
  name: string;
  description: string;
  nodes: CanvasNode[];
  edges: Edge[];
  /**
   * The graph's shared-state channels.
   *
   * Held in the store rather than rebuilt from `emptyGraph()` on every save.
   * Rebuilding it meant a workflow's custom channels were **silently dropped**:
   * loading one did not project them in, and saving wrote the default back over
   * them. The canvas has no editor for this field yet, so the loss was invisible
   * — which is worse than a visible failure, because a channel other nodes read
   * from would simply stop existing in the saved copy.
   */
  stateSchema: StateField[];
  /** Node type definitions from the backend, keyed by type. */
  nodeTypes: Record<string, NodeTypeSpec>;
  portColors: Record<string, string>;

  // ---- ui -----------------------------------------------------------------
  selectedNodeId: string | null;
  selectedEdgeId: string | null;
  /**
   * The region the copilot was pointed at.
   *
   * Deliberately separate from `selectedNodeId` / `selectedEdgeId`: those drive
   * the Inspector and are single-selection by design, while a copilot scope can
   * be a marquee over many nodes. Merging the two made selecting a node for its
   * settings silently rewrite the AI's write scope.
   */
  copilotSelection: { nodes: string[]; edges: string[] };
  /** The copilot record to attach to the next saved version. */
  pendingAiChange: CopilotChangeRecord | null;
  dirty: boolean;
  saving: boolean;
  lastSavedAt: string | null;
  error: string | null;
  issues: ValidationIssue[];
  validating: boolean;
  /** Whether the AI features are usable; drives hiding vs. disabling of affordances. */
  aiStatus: AIStatus | null;

  // ---- run ----------------------------------------------------------------
  runId: string | null;
  runStatus: RunSummary['status'] | null;
  runEvents: RunEvent[];
  runOutputs: Record<string, unknown>;
  runError: string | null;
  runInterrupt: RunSummary['interrupt'] | null;
  /** What the run has spent. `null` before any figure exists, and for a run read
   * back from the database, where spend is not recorded. */
  runUsage: RunUsage | null;
  stepByNode: Record<string, RunStep>;
  /**
   * The copilot's trial run, shown in the Run tab.
   *
   * It is not a RunManager run: it executes the draft directly and is never
   * persisted, so it cannot come from `/runs/history`. It is kept beside — not
   * inside — the normal run state so starting a real run does not erase it.
   */
  selfTest: TrialRunResult | null;

  // ---- history ------------------------------------------------------------
  past: Snapshot[];
  future: Snapshot[];

  // ---- actions ------------------------------------------------------------
  loadCatalogue: () => Promise<void>;
  loadAIStatus: () => Promise<void>;
  setGraph: (graph: WorkflowGraph) => void;
  replaceGraph: (graph: WorkflowGraph) => void;
  /** Replace the shared-state channels. Marks the document dirty. */
  setStateSchema: (fields: StateField[]) => void;
  toGraph: () => WorkflowGraph;
  reset: () => void;

  addNodeFromType: (type: string, position: XYPosition) => CanvasNode | null;
  updateNode: (id: string, patch: Partial<NodeSpec>) => void;
  updateConfig: (id: string, key: string, value: unknown) => void;
  applyConfig: (id: string, config: Record<string, unknown>) => string[];
  removeNode: (id: string) => void;
  duplicateNode: (id: string) => void;

  onNodesChange: (changes: NodeChange<CanvasNode>[]) => void;
  onEdgesChange: (changes: EdgeChange[]) => void;
  onConnect: (connection: Connection) => void;
  isValidConnection: (connection: Connection | Edge) => boolean;
  removeEdge: (id: string) => void;
  setEdgeLabel: (id: string, label: string) => void;

  selectNode: (id: string | null) => void;
  selectEdge: (id: string | null) => void;
  setCopilotSelection: (nodes: string[], edges: string[]) => void;
  setAiChange: (change: CopilotChangeRecord | null) => void;
  /**
   * Apply a copilot snapshot without pushing another undo entry.
   *
   * The copilot pushes one snapshot before it starts, so the whole turn is one
   * undo step. Pushing on every streamed snapshot would make Undo peel the
   * edit back node by node.
   */
  applyLiveGraph: (graph: WorkflowGraph) => void;
  /** Re-arrange nodes left-to-right; one undo step, like any other edit. */
  tidyLayout: (perBand?: number) => Promise<void>;
  setName: (name: string) => void;
  setDescription: (description: string) => void;
  markSaved: () => void;
  setError: (message: string | null) => void;

  validate: () => Promise<void>;
  /** Debounced validate: the edit paths use this instead of `validate`. */
  validateSoon: () => void;
  save: () => Promise<string | null>;

  undo: () => void;
  redo: () => void;
  pushHistory: () => void;

  startRun: (inputs: Record<string, unknown>) => Promise<void>;
  applyRunEvent: (event: RunEvent) => void;
  finishRun: (summary: RunSummary) => void;
  resumeRun: (payload: Record<string, unknown>) => Promise<void>;
  cancelRun: () => Promise<void>;
  clearRun: () => void;
  setSelfTest: (result: TrialRunResult | null) => void;
}

// ---------------------------------------------------------------------------
// Conversion helpers
// ---------------------------------------------------------------------------

export function specToNode(spec: NodeSpec, typeSpec: NodeTypeSpec): CanvasNode {
  // If the node defines dynamic branches (e.g. Condition node), construct the outputs
  // immediately from its config so edges have matching source handles on initial render.
  let dynamicOutputs = typeSpec.outputs;
  if (
    typeSpec.dynamic_outputs === 'from_param' &&
    typeSpec.dynamic_outputs_param &&
    Array.isArray(spec.config?.[typeSpec.dynamic_outputs_param])
  ) {
    const branches = spec.config[typeSpec.dynamic_outputs_param] as { id?: string; label?: string }[];
    dynamicOutputs = branches.map((b) => ({
      id: b.id ?? 'yes',
      label: b.label ?? b.id ?? 'Branch',
      type: 'boolean',
    }));
  }

  return {
    id: spec.id,
    type: 'scaffold',
    position: { x: spec.position.x, y: spec.position.y },
    data: {
      spec,
      color: typeSpec.color,
      icon: typeSpec.icon,
      inputs: typeSpec.inputs.map((p) => ({
        id: p.id,
        label: p.label,
        type: p.type,
        required: p.required,
      })),
      outputs: dynamicOutputs.map((p) => ({ id: p.id, label: p.label, type: p.type })),
    },
    parentId: spec.ui.parent_id ?? undefined,
  };
}

function nodeToSpec(node: CanvasNode): NodeSpec {
  return {
    ...node.data.spec,
    position: { x: Math.round(node.position.x), y: Math.round(node.position.y) },
  };
}

/**
 * An edge handle, accepting both wire spellings.
 *
 * Graphs loaded from the workflow store use React Flow's camelCase. Graphs
 * returned by the AI endpoints are Pydantic `model_dump()` output, which uses
 * the field names — snake_case — because the API does not serialise by alias.
 * Reading only one spelling silently drops the handle and turns a Condition
 * branch into an edge with no source port.
 */
function edgeHandle(
  edge: EdgeSpec,
  key: 'sourceHandle' | 'targetHandle',
): string | undefined {
  const direct = edge[key];
  if (typeof direct === 'string' && direct) return direct;
  const snake = (edge as unknown as Record<string, unknown>)[
    key === 'sourceHandle' ? 'source_handle' : 'target_handle'
  ];
  return typeof snake === 'string' && snake ? snake : undefined;
}

export function specToEdge(edge: EdgeSpec): Edge {
  const sourceHandle = edgeHandle(edge, 'sourceHandle');
  const targetHandle = edgeHandle(edge, 'targetHandle');
  return {
    id: edge.id,
    source: edge.source,
    sourceHandle,
    target: edge.target,
    targetHandle,
    label: edge.label,
    type: 'smoothstep',
    // Loop-back styling is decided at render time, where node positions are
    // available. It used to key on `sourceHandle === 'continue'`, which is the
    // edge *out of* the gate, not the return edge — so the actual cycle was
    // drawn as an ordinary line and, on a single-row layout, lay exactly on top
    // of the forward chain. See `canvas/loopEdges.ts`.
  };
}

function edgeToSpec(edge: Edge): EdgeSpec {
  return {
    id: edge.id,
    source: edge.source,
    sourceHandle: edge.sourceHandle ?? null,
    target: edge.target,
    targetHandle: edge.targetHandle ?? null,
    label: typeof edge.label === 'string' ? edge.label : '',
    type: 'smoothstep',
  };
}

function snapshot(state: WorkflowState): Snapshot {
  return {
    // Structured clone so a later mutation cannot reach back into history.
    nodes: structuredClone(state.nodes),
    edges: structuredClone(state.edges),
    graphId: state.graphId,
    name: state.name,
    description: state.description,
  };
}

/**
 * Project a graph spec onto the store's canvas fields.
 *
 * Shared by `setGraph` and `replaceGraph` because the two differ only in what
 * happens *around* the projection — one clears history because it is loading a
 * different document, the other preserves it because it is editing this one.
 * Duplicating the mapping is how those two would drift apart.
 */
function graphFields(
  graph: WorkflowGraph,
  nodeTypes: Record<string, NodeTypeSpec>,
): Partial<WorkflowState> {
  return {
    graphId: graph.id,
    name: graph.name,
    description: graph.description,
    stateSchema: graph.state_schema,
    nodes: graph.nodes
      .filter((spec) => nodeTypes[spec.type])
      .map((spec) => specToNode(spec, nodeTypes[spec.type]!)),
    edges: graph.edges.map(specToEdge),
    selectedNodeId: null,
    selectedEdgeId: null,
    copilotSelection: { nodes: [], edges: [] },
  pendingAiChange: null,
    error: null,
    issues: [],
    ...clearedRun(),
    // A trial belongs to the document it was run against.
    selfTest: null,
  };
}

// ---------------------------------------------------------------------------
// Store
// ---------------------------------------------------------------------------

/**
 * The document a fresh editor session starts from.
 *
 * Built once rather than spread inline in the store literal: `emptyGraph()`
 * returns a `WorkflowGraph`, whose id field is named `id`, whereas this store
 * names it `graphId`. Spreading would therefore leave `graphId` unset.
 */
const INITIAL_GRAPH = emptyGraph();

export const useWorkflow = create<WorkflowState>((set, get) => ({
  graphId: INITIAL_GRAPH.id,
  // Translated rather than taken from `emptyGraph`, so a new workflow is named
  // in the user's language instead of always in English.
  name: translate('app.untitled'),
  description: INITIAL_GRAPH.description,
  nodes: [],
  edges: [],
  stateSchema: INITIAL_GRAPH.state_schema,
  nodeTypes: {},
  portColors: {},

  selectedNodeId: null,
  selectedEdgeId: null,
  copilotSelection: { nodes: [], edges: [] },
  pendingAiChange: null,
  dirty: false,
  saving: false,
  lastSavedAt: null,
  error: null,
  issues: [],
  validating: false,
  aiStatus: null,

  runId: null,
  runStatus: null,
  runEvents: [],
  runOutputs: {},
  runError: null,
  runInterrupt: null,
  runUsage: null,
  stepByNode: {},
  selfTest: null,

  past: [],
  future: [],

  // -------------------------------------------------------------------------
  // Catalogue
  // -------------------------------------------------------------------------

  loadCatalogue: async () => {
    try {
      const response = await api.nodeTypes();
      const nodeTypes: Record<string, NodeTypeSpec> = {};
      for (const spec of response.types) nodeTypes[spec.type] = spec;
      set({ nodeTypes, portColors: response.port_colors });
    } catch (error) {
      set({
        error: error instanceof Error ? error.message : translate('error.loadNodes'),
      });
    }
  },

  loadAIStatus: async () => {
    try {
      set({ aiStatus: await api.aiStatus() });
    } catch {
      // The AI panel degrades to hidden rather than showing an error: if the
      // status endpoint is unreachable the app has bigger problems to report.
      set({ aiStatus: null });
    }
  },

  // -------------------------------------------------------------------------
  // Document
  // -------------------------------------------------------------------------

  setGraph: (graph) => {
    // Loading a different document. History is cleared on purpose: undoing into
    // the previously-open workflow would be nonsense.
    set({
      ...graphFields(graph, get().nodeTypes),
      dirty: false,
      // A document loaded from disk is saved; without this the header showed
      // an empty state right after opening a workflow, which made "is my work
      // saved?" impossible to answer.
      lastSavedAt: graph.updated_at ?? null,
      past: [],
      future: [],
    });
    get().validateSoon();
  },

  replaceGraph: (graph) => {
    // Applying an AI draft is an edit like any other, so it must be undoable and
    // must leave the document dirty. It therefore cannot route through
    // `setGraph`, which clears history and marks the document saved — doing so
    // silently made every AI-applied change un-undoable and appeared saved, so a
    // user who applied a bad repair and closed the tab lost it without warning.
    get().pushHistory();
    set({ ...graphFields(graph, get().nodeTypes), dirty: true });
    get().validateSoon();
  },

  toGraph: () => {
    const state = get();
    return {
      id: state.graphId,
      name: state.name,
      description: state.description,
      version: 1,
      nodes: state.nodes.map(nodeToSpec),
      edges: state.edges.map(edgeToSpec),
      viewport: { x: 0, y: 0, zoom: 1 },
        state_schema: state.stateSchema,
      };
    },

    setStateSchema: (fields) => set({ stateSchema: fields, dirty: true }),
  reset: () => {
    const fresh = emptyGraph();
    set({
      graphId: fresh.id,
      name: translate('app.untitled'),
      description: fresh.description,
      nodes: [],
      edges: [],
      stateSchema: fresh.state_schema,
      selectedNodeId: null,
      selectedEdgeId: null,
      copilotSelection: { nodes: [], edges: [] },
  pendingAiChange: null,
      dirty: false,
      error: null,
      issues: [],
      past: [],
      future: [],
      ...clearedRun(),
      selfTest: null,
    });
  },

  // -------------------------------------------------------------------------
  // Nodes
  // -------------------------------------------------------------------------

  addNodeFromType: (type, position) => {
    const { nodeTypes } = get();
    const typeSpec = nodeTypes[type];
    if (!typeSpec) {
      set({ error: translate('error.unknownNodeType', { type }) });
      return null;
    }
    if (typeSpec.disabled) {
      set({
        error:
          typeSpec.disabled_reason ||
          translate('error.disabledNodeType', { type }),
      });
      return null;
    }

    get().pushHistory();
    const spec = createNode(typeSpec, position);
    const node = specToNode(spec, typeSpec);

    set((state) => ({
      nodes: [...state.nodes, node],
      selectedNodeId: node.id,
      selectedEdgeId: null,
      dirty: true,
    }));
    get().validateSoon();
    return node;
  },

  updateNode: (id, patch) => {
    set((state) => ({
      nodes: state.nodes.map((node) =>
        node.id === id
          ? { ...node, data: { ...node.data, spec: { ...node.data.spec, ...patch } } }
          : node,
      ),
      dirty: true,
    }));
    get().validateSoon();
  },

  updateConfig: (id, key, value) => {
    get().pushHistory();
    set((state) => ({
      nodes: state.nodes.map((node) => {
        if (node.id !== id) return node;
        const spec = node.data.spec;
        const config = { ...spec.config };

        // An emptied field should read as "unset", not as `""` — otherwise the
        // backend's required-param check cannot tell a cleared value from an
        // unset one, and the declared default would silently apply.
        if (value === '' || value === undefined) delete config[key];
        else config[key] = value;

        return { ...node, data: { ...node.data, spec: { ...spec, config } } };
      }),
      dirty: true,
    }));
    get().validateSoon();
  },

  /**
   * Apply several config values at once, as one edit.
   *
   * The assistant proposes a whole node's worth of settings, so writing them
   * one `updateConfig` at a time would put a dozen entries on the undo stack for
   * one action — pressing undo would peel the suggestion back field by field.
   * One history push makes it a single step the user can take back, and the
   * returning list of changed keys lets a caller say what it actually touched
   * rather than claiming it filled something it left alone.
   */
  applyConfig: (id, config) => {
    const node = get().nodes.find((candidate) => candidate.id === id);
    if (!node) return [];

    const current = node.data.spec.config ?? {};
    const next: Record<string, unknown> = { ...current };
    const changed: string[] = [];

    for (const [key, value] of Object.entries(config)) {
      // Same rule as `updateConfig`: an empty value means "unset", so the
      // declared default applies rather than an empty string being stored.
      const normalised = value === '' || value === undefined ? undefined : value;
      const existing = current[key];
      const same =
        normalised === undefined
          ? !(key in current)
          : JSON.stringify(existing) === JSON.stringify(normalised);
      if (same) continue;

      if (normalised === undefined) delete next[key];
      else next[key] = normalised;
      changed.push(key);
    }

    if (changed.length === 0) return [];

    get().pushHistory();
    set((state) => ({
      nodes: state.nodes.map((candidate) =>
        candidate.id === id
          ? {
              ...candidate,
              data: {
                ...candidate.data,
                spec: { ...candidate.data.spec, config: next },
              },
            }
          : candidate,
      ),
      dirty: true,
    }));
    get().validateSoon();
    return changed;
  },

  removeNode: (id) => {
    get().pushHistory();
    set((state) => ({
      nodes: state.nodes.filter((node) => node.id !== id),
      // React Flow requires edges to be removed with their node, or it renders
      // dangling paths.
      edges: state.edges.filter((edge) => edge.source !== id && edge.target !== id),
      selectedNodeId: state.selectedNodeId === id ? null : state.selectedNodeId,
      dirty: true,
    }));
    get().validateSoon();
  },

  duplicateNode: (id) => {
    const { nodes, nodeTypes } = get();
    const source = nodes.find((node) => node.id === id);
    if (!source) return;

    const typeSpec = nodeTypes[source.data.spec.type];
    if (!typeSpec) return;

    get().pushHistory();
    const copy = createNode(typeSpec, {
      x: source.position.x + 40,
      y: source.position.y + 40,
    });
    copy.label = `${source.data.spec.label} copy`;
    copy.description = source.data.spec.description;
    copy.config = structuredClone(source.data.spec.config);
    copy.trusted = source.data.spec.trusted;

    set((state) => ({
      nodes: [...state.nodes, specToNode(copy, typeSpec)],
      selectedNodeId: copy.id,
      dirty: true,
    }));
    // Duplicating a Start node is a validation error; the badge must appear
    // without waiting for an unrelated edit.
    get().validateSoon();
  },

  // -------------------------------------------------------------------------
  // React Flow event handlers
  // -------------------------------------------------------------------------

  onNodesChange: (changes) => {
    const removed = changes.some((change) => change.type === 'remove');
    const moved = changes.some(
      (change) => change.type === 'position' && change.position !== undefined,
    );
    const dragStarted = changes.some(
      (change) => change.type === 'position' && change.dragging === true,
    );
    const dragEnded = changes.some(
      (change) => change.type === 'position' && change.dragging === false,
    );

    // One undo entry per drag, pushed *before* the first position is applied so
    // the snapshot holds the pre-drag layout. Without this a moved node looked
    // saved, was not undoable, and a reload silently put it back.
    if (dragStarted) {
      const now = Date.now();
      if (!positionDrag.active || now - positionDrag.at > 1500) {
        positionDrag = { active: true, at: now };
        get().pushHistory();
      }
    }
    if (dragEnded) positionDrag = { active: false, at: 0 };

    if (removed) get().pushHistory();

    const structural = changes.some(
      (change) => change.type !== 'position' && change.type !== 'select' && change.type !== 'dimensions',
    );

    set((state) => ({
      nodes: applyNodeChanges(changes, state.nodes),
      dirty: structural || moved ? true : state.dirty,
    }));

    // Delete is a graph change like any other: its validation must not wait for
    // the next unrelated edit.
    if (removed) get().validateSoon();
  },

  onEdgesChange: (changes) => {
    if (changes.some((change) => change.type === 'remove')) get().pushHistory();
    set((state) => ({
      edges: applyEdgeChanges(changes, state.edges),
      dirty: true,
    }));
    get().validateSoon();
  },

  onConnect: (connection) => {
    if (!get().isValidConnection(connection)) return;

    get().pushHistory();
    set((state) => ({
      edges: addEdge(
        { ...connection, id: `e-${connection.source}-${connection.target}-${Date.now()}` },
        state.edges,
      ),
      dirty: true,
    }));
    get().validateSoon();
  },

  isValidConnection: (connection) => {
    const { nodes } = get();

    if (!connection.source || !connection.target) return false;
    if (connection.source === connection.target) return false;

    const source = nodes.find((node) => node.id === connection.source);
    const target = nodes.find((node) => node.id === connection.target);
    if (!source || !target) return false;

    const output = source.data.outputs.find((port) => port.id === connection.sourceHandle);
    const input = target.data.inputs.find((port) => port.id === connection.targetHandle);

    // Unknown handles mean the ports have not resolved yet; let React Flow decide
    // rather than blocking a legitimate connection.
    if (!output || !input) return true;

    return portsCompatible(output.type, input.type);
  },

  removeEdge: (id) => {
    get().pushHistory();
    set((state) => ({
      edges: state.edges.filter((edge) => edge.id !== id),
      selectedEdgeId: null,
      dirty: true,
    }));
    get().validateSoon();
  },

  setEdgeLabel: (id, label) => {
    set((state) => ({
      edges: state.edges.map((edge) => (edge.id === id ? { ...edge, label } : edge)),
      dirty: true,
    }));
  },

  // -------------------------------------------------------------------------
  // Selection & metadata
  // -------------------------------------------------------------------------

  selectNode: (id) => set({ selectedNodeId: id, selectedEdgeId: null }),
  selectEdge: (id) => set({ selectedEdgeId: id, selectedNodeId: null }),

  setCopilotSelection: (nodes, edges) => {
    // React Flow fires selection changes continuously while a marquee is being
    // dragged. Writing an identical object each time would re-render every
    // subscriber for a selection that did not change.
    const current = get().copilotSelection;
    const same =
      current.nodes.length === nodes.length &&
      current.edges.length === edges.length &&
      current.nodes.every((id, index) => id === nodes[index]) &&
      current.edges.every((id, index) => id === edges[index]);
    if (!same) set({ copilotSelection: { nodes, edges } });
  },

  setAiChange: (change) => set({ pendingAiChange: change }),

  applyLiveGraph: (graph) => {
    const state = get();
    set({
      ...graphFields(graph, state.nodeTypes),
      // A streamed snapshot is an edit, not a document switch: the user's
      // selection (and the copilot's write scope) must survive it.
      selectedNodeId: state.selectedNodeId,
      selectedEdgeId: state.selectedEdgeId,
      copilotSelection: state.copilotSelection,
      dirty: true,
    });
  },

  tidyLayout: async (perBand = 6) => {
    if (!get().nodes.length) return;
    get().pushHistory();
    try {
      const { graph } = await api.tidyLayout(get().toGraph(), perBand);
      get().applyLiveGraph(graph);
      // The canvas listens for this and frames the re-arranged graph, so the
      // user sees the result instead of having to hunt for it.
      window.dispatchEvent(new CustomEvent('scaffold:fit-view'));
    } catch (error) {
      set({
        error:
          error instanceof Error
            ? error.message
            : translate('error.layoutFailed'),
      });
    }
  },

  setName: (name) => set({ name, dirty: true }),
  setDescription: (description) => set({ description, dirty: true }),
  markSaved: () => set({ dirty: false, saving: false, lastSavedAt: new Date().toISOString() }),
  setError: (message) => set({ error: message }),

  // -------------------------------------------------------------------------
  // Validation & persistence
  // -------------------------------------------------------------------------

  validate: async () => {
    set({ validating: true });
    try {
      const report = await api.validate(get().toGraph());
      set({ issues: report.issues, validating: false });
    } catch (error) {
      // A validation failure is not worth blocking the UI over — the graph may
      // simply not be saveable yet.
      set({
        validating: false,
        error: error instanceof Error ? error.message : translate('error.validateFailed'),
      });
    }
  },

  validateSoon: () => {
    if (validateTimer !== null) clearTimeout(validateTimer);
    validateTimer = setTimeout(() => {
      validateTimer = null;
      void get().validate();
    }, 220);
  },

  save: async () => {
    set({ saving: true, error: null });
    try {
      const graph = get().toGraph();
      const change = get().pendingAiChange;
      const saved = change
        ? await api.saveWorkflowWithChange(
            graph,
            change as unknown as Record<string, unknown>,
          )
        : await api.saveWorkflow(graph);
      set({
        saving: false,
        dirty: false,
        pendingAiChange: null,
        lastSavedAt: new Date().toISOString(),
      });
      return saved.id;
    } catch (error) {
      set({
        saving: false,
        error: error instanceof Error ? error.message : translate('error.saveFailed'),
      });
      return null;
    }
  },

  // -------------------------------------------------------------------------
  // History
  // -------------------------------------------------------------------------

  pushHistory: () => {
    const state = get();
    const past = [...state.past, snapshot(state)].slice(-HISTORY_LIMIT);
    set({ past, future: [] });
  },

  undo: () => {
    const state = get();
    const previous = state.past.at(-1);
    if (!previous) return;

    set({
      past: state.past.slice(0, -1),
      future: [...state.future, snapshot(state)].slice(-HISTORY_LIMIT),
      nodes: previous.nodes,
      edges: previous.edges,
      graphId: previous.graphId,
      name: previous.name,
      description: previous.description,
      selectedNodeId: null,
      selectedEdgeId: null,
      dirty: true,
    });
    get().validateSoon();
  },

  redo: () => {
    const state = get();
    const next = state.future.at(-1);
    if (!next) return;

    set({
      future: state.future.slice(0, -1),
      past: [...state.past, snapshot(state)].slice(-HISTORY_LIMIT),
      nodes: next.nodes,
      edges: next.edges,
      graphId: next.graphId,
      name: next.name,
      description: next.description,
      selectedNodeId: null,
      selectedEdgeId: null,
      dirty: true,
    });
    get().validateSoon();
  },

  // -------------------------------------------------------------------------
  // Runs
  // -------------------------------------------------------------------------

  startRun: async (inputs) => {
    set({
      ...clearedRun(),
      runStatus: 'running',
      runEvents: [],
      stepByNode: {},
    });
    try {
      const summary = await api.startRun({ graph: get().toGraph(), inputs });
      set({ runId: summary.id, runStatus: summary.status });
    } catch (error) {
      set({
        runStatus: 'failed',
        runError: error instanceof Error ? error.message : translate('error.startFailed'),
      });
    }
  },

  applyRunEvent: (event) => {
    set((state) => {
      const stepByNode = { ...state.stepByNode };
      let runInterrupt = state.runInterrupt;
      let runError = state.runError;
      let runOutputs = state.runOutputs;
      let runStatus = state.runStatus;
      let runUsage = state.runUsage;

      switch (event.type) {
        case 'node_output_chunk':
          // Model nodes report what they spent on a chunk event. Accumulated
          // here so the panel can show a running total while the run is live,
          // rather than only after it ends.
          if (event.data.usage) {
            const reported = event.data.usage as {
              total_tokens?: number;
              prompt_tokens?: number;
              completion_tokens?: number;
              calls?: number;
            };
            const previous = runUsage ?? {
              tokens: 0,
              tokens_known: true,
              steps: 0,
              elapsed_seconds: 0,
            };
            runUsage = {
              ...previous,
              tokens: previous.tokens + (reported.total_tokens ?? 0),
            };
          }
          break;

        case 'run_queued':
          // The run is accepted but waiting for a free slot. Without this the UI
          // shows a run that looks stuck for no reason, which is the whole point
          // of having a distinct status.
          runStatus = 'queued';
          break;

        case 'node_started':
          stepByNode[event.node_id ?? ''] = {
            node_id: event.node_id ?? '',
            node_label: event.node_label ?? '',
            index: Object.keys(stepByNode).length + 1,
            status: 'running',
            inputs: (event.data.inputs as Record<string, unknown>) ?? null,
            outputs: null,
            error: null,
            duration_ms: null,
          };
          break;

        case 'node_finished':
          if (event.node_id && stepByNode[event.node_id]) {
            stepByNode[event.node_id] = {
              ...stepByNode[event.node_id]!,
              status: 'succeeded',
              outputs: (event.data.outputs as Record<string, unknown>) ?? null,
              duration_ms: (event.data.duration_ms as number) ?? null,
            };
          }
          break;

        case 'node_failed':
          if (event.node_id) {
            stepByNode[event.node_id] = {
              ...(stepByNode[event.node_id] ?? {
                node_id: event.node_id,
                node_label: event.node_label ?? '',
                index: Object.keys(stepByNode).length + 1,
                inputs: null,
                outputs: null,
              }),
              status: 'failed',
              error: (event.data.error as string) ?? 'Node failed.',
              duration_ms: (event.data.duration_ms as number) ?? null,
            } as RunStep;
          }
          break;

        case 'run_started':
          runStatus = 'running';
          runInterrupt = null;
          break;

        case 'interrupt_raised':
          runInterrupt = (event.data.interrupt as RunSummary['interrupt']) ?? null;
          runStatus = 'interrupted';
          break;

        case 'run_finished':
          runOutputs = (event.data.outputs as Record<string, unknown>) ?? {};
          runStatus = 'succeeded';
          runInterrupt = null;
          // The authoritative total: chunk events can be lost or unordered, and
          // the budget already summed every model call.
          if (event.data.usage) runUsage = event.data.usage as typeof runUsage;
          break;

        case 'run_failed':
          runError = (event.data.error as string) ?? 'The run failed.';
          runStatus = 'failed';
          runInterrupt = null;
          if (event.data.usage) runUsage = event.data.usage as typeof runUsage;
          break;

        default:
          break;
      }

      // Keep the stream bounded: chunks are high-volume and each carries the
      // full text so far, so only the tail is worth retaining.
      const runEvents = [...state.runEvents, event].slice(-500);

      return {
        runEvents,
        stepByNode,
        runInterrupt,
        runError,
        runOutputs,
        runStatus,
        runUsage,
      };
    });
  },

  finishRun: (summary) => {
    set({
      runStatus: summary.status,
      runOutputs: summary.outputs,
      runError: summary.error,
      runInterrupt: summary.interrupt ?? null,
      // The authoritative figure: the server counted it, including usage from
      // nodes this client never saw events for.
      runUsage: summary.usage ?? null,
    });
  },

  resumeRun: async (payload) => {
    const { runId } = get();
    if (!runId) return;
    try {
      set({ runInterrupt: null });
      const summary = await api.resumeRun(runId, payload);
      set({
        runStatus: summary.status,
        runOutputs: summary.outputs,
        runInterrupt: summary.interrupt ?? null,
        runUsage: summary.usage ?? null,
      });
      window.dispatchEvent(new CustomEvent('scaffold:run-resumed', { detail: { runId } }));
      try {
        const { steps } = await api.getRunSteps(runId);
        if (steps) {
          const stepByNode: Record<string, RunStep> = {};
          steps.forEach((s) => {
            stepByNode[s.node_id] = s;
          });
          set({ stepByNode });
        }
      } catch {
        // Ignore step refresh errors
      }
    } catch (error) {
      set({
        runInterrupt: null,
        runError: error instanceof Error ? error.message : translate('error.resumeFailed'),
      });
      try {
        const fresh = await api.getRun(runId);
        const { steps } = await api.getRunSteps(runId);
        const stepByNode: Record<string, RunStep> = {};
        if (steps) {
          steps.forEach((s) => {
            stepByNode[s.node_id] = s;
          });
        }
        set({
          runStatus: fresh.status,
          runOutputs: fresh.outputs,
          runInterrupt: fresh.interrupt ?? null,
          stepByNode: Object.keys(stepByNode).length > 0 ? stepByNode : get().stepByNode,
        });
      } catch {
        // Keep reported error if fresh read fails
      }
    }
  },

  cancelRun: async () => {
    const { runId } = get();
    if (!runId) return;
    try {
      const summary = await api.cancelRun(runId);
      set({ runStatus: summary.status });
    } catch (error) {
      set({ runError: error instanceof Error ? error.message : translate('error.cancelFailed') });
    }
  },

  clearRun: () => set(clearedRun()),
  setSelfTest: (result) => set({ selfTest: result }),
}));

function clearedRun() {
  return {
    runId: null,
    runStatus: null,
    runEvents: [],
    runOutputs: {},
    runError: null,
    runInterrupt: null,
    runUsage: null,
    stepByNode: {},
  };
}

// ---------------------------------------------------------------------------
// Selectors
// ---------------------------------------------------------------------------

export function selectSelectedNode(state: WorkflowState): CanvasNode | null {
  if (!state.selectedNodeId) return null;
  return state.nodes.find((node) => node.id === state.selectedNodeId) ?? null;
}

/**
 * Split validation issues by severity.
 *
 * Plain helpers rather than store selectors, and that distinction matters:
 * `filter` returns a *new array* on every call, and a store selector that
 * returns a fresh reference each time makes `useSyncExternalStore` believe the
 * snapshot changed — which re-renders, allocates again, and spins forever
 * ("The result of getSnapshot should be cached to avoid an infinite loop").
 *
 * Call sites therefore subscribe to the stable `issues` array and memoize the
 * split themselves.
 */
export function selectErrors(issues: ValidationIssue[]): ValidationIssue[] {
  return issues.filter((issue) => issue.severity === 'error');
}

export function selectWarnings(issues: ValidationIssue[]): ValidationIssue[] {
  return issues.filter((issue) => issue.severity === 'warning');
}

export function canUndo(state: WorkflowState): boolean {
  return state.past.length > 0;
}

export function canRedo(state: WorkflowState): boolean {
  return state.future.length > 0;
}
