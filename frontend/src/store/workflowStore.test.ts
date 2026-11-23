/**
 * Store invariants around applying an AI draft to the canvas.
 *
 * These exist because two of them were silently broken and nothing noticed:
 *
 *  * `replaceGraph` called `setGraph`, which clears `past`/`future` because it
 *    is meant for *loading* a different document. The result was that every
 *    AI-applied change — both "replace canvas" in the generate dialog and
 *    "apply fix" in the repair dialog — was un-undoable, while the UI promised
 *    Ctrl+Z worked.
 *  * `setGraph` also sets `dirty: false`, so applying a draft marked the
 *    document *saved*. A user who accepted a bad repair and closed the tab lost
 *    it with no warning.
 *
 * Neither is visible from the canvas, and neither shows up in a type error, so
 * they are exactly the kind of thing a store test has to pin down.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { NodeTypeSpec, WorkflowGraph } from '@/types/workflow';

// The store calls the API on nearly every mutation. Validation is not what is
// under test here, and letting jsdom attempt real fetches would make the suite
// slow and flaky, so the whole client is stubbed.
vi.mock('@/api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    validate: vi.fn(async () => ({ ok: true, issues: [] })),
    nodeTypes: vi.fn(async () => ({ types: [], port_colors: {} })),
    tools: vi.fn(async () => ({ tools: [] })),
    stateFields: vi.fn(async () => ({ reducers: [], type_labels: {} })),
    aiStatus: vi.fn(async () => ({ enabled: false })),
    savedWorkflows: vi.fn(async () => ({ workflows: [] })),
    tidyLayout: vi.fn(async (incoming: WorkflowGraph) => ({
      graph: {
        ...incoming,
        nodes: incoming.nodes.map((node, index) => ({
          ...node,
          position: { x: index * 340, y: 0 },
        })),
      },
    })),
  },
}));

import { canRedo, canUndo, useWorkflow } from '@/store/workflowStore';

/** Minimal catalogue entry — enough for `specToNode` to project a node. */
const START_SPEC: NodeTypeSpec = {
  type: 'start',
  label: 'Start',
  description: '',
  category: 'input',
  color: '#000',
  icon: 'play',
  inputs: [],
  outputs: [{ id: 'out', label: 'Out', type: 'any' }],
  params: [],
  dynamic_outputs: 'none',
  dynamic_outputs_param: null,
  is_entry: true,
  is_terminal: false,
  is_container: false,
  supports_streaming: false,
  requires_trust: false,
};

const END_SPEC: NodeTypeSpec = {
  ...START_SPEC,
  type: 'end',
  label: 'End',
  category: 'output',
  inputs: [{ id: 'in', label: 'In', type: 'any', required: false }],
  outputs: [],
  is_entry: false,
  is_terminal: true,
};

function graph(nodeIds: string[], id = 'wf-1', name = 'Workflow'): WorkflowGraph {
  return {
    id,
    name,
    description: '',
    version: 1,
    nodes: nodeIds.map((nodeId, index) => ({
      id: nodeId,
      type: 'start',
      label: nodeId,
      description: '',
      position: { x: index * 100, y: 0 },
      config: {},
      ui: {},
      trusted: false,
    })),
    edges: [],
    viewport: { x: 0, y: 0, zoom: 1 },
    state_schema: [],
  } as unknown as WorkflowGraph;
}

beforeEach(() => {
  useWorkflow.setState({
    nodeTypes: { start: START_SPEC, end: END_SPEC },
    nodes: [],
    edges: [],
    past: [],
    future: [],
    graphId: 'initial',
    name: 'Untitled',
    description: '',
    dirty: false,
    issues: [],
  });
});

describe('replaceGraph (applying an AI draft)', () => {
  it('is undoable', () => {
    // Seed one node so there is something to go back to.
    useWorkflow.getState().setGraph(graph(['a']));
    expect(useWorkflow.getState().nodes).toHaveLength(1);

    useWorkflow.getState().replaceGraph(graph(['a', 'b']));
    expect(useWorkflow.getState().nodes).toHaveLength(2);

    expect(canUndo(useWorkflow.getState())).toBe(true);

    useWorkflow.getState().undo();
    const after = useWorkflow.getState();
    expect(after.nodes).toHaveLength(1);
    expect(after.nodes[0]!.id).toBe('a');
  });

  it('can be redone after undoing', () => {
    useWorkflow.getState().setGraph(graph(['a']));
    useWorkflow.getState().replaceGraph(graph(['a', 'b']));

    useWorkflow.getState().undo();
    expect(canRedo(useWorkflow.getState())).toBe(true);

    useWorkflow.getState().redo();
    expect(useWorkflow.getState().nodes).toHaveLength(2);
  });

  it('leaves the document dirty', () => {
    // Applying a draft changes the canvas, so claiming it is saved would let the
    // user close the tab and lose it without a prompt.
    useWorkflow.getState().setGraph(graph(['a']));
    expect(useWorkflow.getState().dirty).toBe(false);

    useWorkflow.getState().replaceGraph(graph(['a', 'b']));
    expect(useWorkflow.getState().dirty).toBe(true);
  });

  it('adopts the incoming graph identity', () => {
    useWorkflow.getState().setGraph(graph(['a'], 'wf-1', 'First'));
    useWorkflow.getState().replaceGraph(graph(['a'], 'wf-1', 'Renamed by AI'));

    const state = useWorkflow.getState();
    expect(state.graphId).toBe('wf-1');
    expect(state.name).toBe('Renamed by AI');
  });
});

describe('tidyLayout', () => {
  it('applies the server layout as one undoable edit', async () => {
    useWorkflow.getState().setGraph(graph(['a', 'b']));
    expect(useWorkflow.getState().nodes.map((node) => node.position.x)).toEqual([
      0, 100,
    ]);

    await useWorkflow.getState().tidyLayout();

    expect(useWorkflow.getState().nodes.map((node) => node.position.x)).toEqual([
      0, 340,
    ]);
    expect(useWorkflow.getState().dirty).toBe(true);
    expect(canUndo(useWorkflow.getState())).toBe(true);

    useWorkflow.getState().undo();
    expect(useWorkflow.getState().nodes.map((node) => node.position.x)).toEqual([
      0, 100,
    ]);
  });
});

describe('setGraph (loading a document)', () => {
  it('clears history, because undoing into the previous document is nonsense', () => {
    useWorkflow.getState().setGraph(graph(['a']));
    useWorkflow.getState().replaceGraph(graph(['a', 'b']));
    expect(canUndo(useWorkflow.getState())).toBe(true);

    useWorkflow.getState().setGraph(graph(['x'], 'wf-2', 'Other'));

    const state = useWorkflow.getState();
    expect(canUndo(state)).toBe(false);
    expect(canRedo(state)).toBe(false);
    expect(state.dirty).toBe(false);
  });
});

describe('reset (new workflow)', () => {
  it('mints a new graph id so saving cannot overwrite the old workflow', () => {
    useWorkflow.getState().setGraph(graph(['a'], 'wf-1', 'First'));
    const before = useWorkflow.getState().graphId;

    useWorkflow.getState().reset();
    const after = useWorkflow.getState().graphId;

    expect(after).not.toBe(before);
    expect(useWorkflow.getState().nodes).toHaveLength(0);
  });
});

describe('state_schema (shared-state channels)', () => {
  it('survives a load and a save', () => {
    // This was silent data loss. `graphFields` did not read the field, so loading
    // a workflow that declared its own channels dropped them; `toGraph` then wrote
    // `emptyGraph()`'s default back over them on save. There is no editor for the
    // field on the canvas, so nothing on screen ever disagreed — a node that read
    // from a custom channel would simply stop finding it after a save.
    const withChannels = {
      ...graph(['a'], 'wf-channels', 'Has channels'),
      state_schema: [
        { name: 'vars', type: 'json', reducer: 'merge_dict', default: {}, description: '' },
        { name: 'notes', type: 'list', reducer: 'append', default: [], description: 'running notes' },
      ],
    } as unknown as WorkflowGraph;

    useWorkflow.getState().setGraph(withChannels);

    const loaded = useWorkflow.getState();
    expect(loaded.stateSchema.map((field) => field.name)).toEqual(['vars', 'notes']);

    // And the round trip: what `toGraph` hands to the API must still have them.
    expect(loaded.toGraph().state_schema.map((field) => field.name)).toEqual(['vars', 'notes']);
  });

  it('reset restores the default channels rather than keeping the old ones', () => {
    useWorkflow.getState().setGraph({
      ...graph(['a']),
      state_schema: [
        { name: 'stale', type: 'json', reducer: 'overwrite', default: null, description: '' },
      ],
    } as unknown as WorkflowGraph);
    useWorkflow.getState().reset();

    const names = useWorkflow.getState().stateSchema.map((field) => field.name);
    expect(names).not.toContain('stale');
    expect(names).toContain('vars');
  });
});
