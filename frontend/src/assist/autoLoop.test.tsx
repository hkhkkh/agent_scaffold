/**
 * Auto mode driven end to end, against the real store.
 *
 * The unit tests next door pin the rules; these pin the *loop* — which is where
 * the interesting failure was. Opening a saved workflow in auto mode made the
 * assistant spend money on every node that already had a description, because
 * the "what have I already handled" map was seeded once when the hook mounted
 * and the document can be replaced at any time afterwards. Nothing about that
 * is visible in a unit test of the ownership rule.
 */

import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { NodeTypeSpec, WorkflowGraph } from '@/types/workflow';

const fillNode = vi.fn(async (_body: { graph: unknown; node_id: string }) => ({
  config: { prompt: '{{ vars.start.out }}', max_tokens: 2048 },
  rationale: '',
  warnings: [],
  changes: [],
}));
vi.mock('@/api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    validate: vi.fn(async () => ({ ok: true, issues: [] })),
    nodeTypes: vi.fn(async () => ({ types: [], port_colors: {} })),
    tools: vi.fn(async () => ({ tools: [] })),
    stateFields: vi.fn(async () => ({ reducers: [], type_labels: {} })),
    aiStatus: vi.fn(async () => ({ enabled: false })),
    savedWorkflows: vi.fn(async () => ({ workflows: [] })),
    fillNode: (body: { graph: unknown; node_id: string }) => fillNode(body),
  },
}));

import {
  assistKeysInPlace,
  assistNoteApplies,
  useAssistNotes,
  useAutoAssist,
} from '@/assist/auto';
import { setAssistMode } from '@/assist/mode';
import { useWorkflow } from '@/store/workflowStore';

const LLM_SPEC: NodeTypeSpec = {
  type: 'llm',
  label: 'LLM',
  description: '',
  category: 'model',
  color: '#000',
  icon: 'sparkles',
  inputs: [],
  outputs: [{ id: 'out', label: 'Out', type: 'text' }],
  params: [],
  dynamic_outputs: 'none',
  dynamic_outputs_param: null,
  is_entry: false,
  is_terminal: false,
  is_container: false,
  supports_streaming: true,
  requires_trust: false,
};

function doc(id: string, nodes: { id: string; description: string }[]): WorkflowGraph {
  return {
    id,
    name: 'Doc',
    description: '',
    version: 1,
    nodes: nodes.map((node, index) => ({
      id: node.id,
      type: 'llm',
      label: node.id,
      description: node.description,
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

const DEBOUNCE = 2000;

/** Let the debounce elapse and the mocked request settle. */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(DEBOUNCE + 100);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  fillNode.mockClear();
  window.localStorage.clear();
  // Nothing is mounted yet, so this cannot disturb a live hook.
  setAssistMode('auto');
  useWorkflow.setState({
    nodeTypes: { llm: LLM_SPEC },
    nodes: [],
    edges: [],
    past: [],
    future: [],
    graphId: 'empty',
    name: 'Untitled',
    description: '',
    dirty: false,
    issues: [],
    runId: null,
    validating: false,
  });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('auto mode against the store', () => {
  it('does not spend anything on a workflow that was already described', async () => {
    // The hook mounts on an empty canvas — which is what the app does — and the
    // user then opens a document. Every description in it is old news.
    renderHook(() => useAutoAssist());

    act(() => {
      useWorkflow.getState().setGraph(
        doc('wf-1', [
          { id: 'a', description: '第一步：接收用户的问题' },
          { id: 'b', description: '第二步：给出中文回答' },
        ]),
      );
    });
    await settle();

    expect(fillNode).not.toHaveBeenCalled();
  });

  it('fills a step whose description the user writes after the document is open', async () => {
    renderHook(() => useAutoAssist());

    act(() => {
      useWorkflow.getState().setGraph(doc('wf-1', [{ id: 'a', description: '旧的描述' }]));
    });
    await settle();
    fillNode.mockClear();

    act(() => {
      useWorkflow.getState().updateNode('a', { description: '把需求改写成任务清单' });
    });
    await settle();

    expect(fillNode).toHaveBeenCalledTimes(1);
    expect(fillNode.mock.calls[0]?.[0]).toMatchObject({ node_id: 'a' });
  });

  it('writes what came back onto the node, and leaves a note saying so', async () => {
    const { result } = renderHook(() => ({
      assist: useAutoAssist(),
      notes: useAssistNotes(),
    }));

    act(() => {
      useWorkflow.getState().setGraph(doc('wf-1', [{ id: 'a', description: '旧' }]));
    });
    await settle();

    act(() => {
      useWorkflow.getState().updateNode('a', { description: '写成中文详细回答' });
    });
    await settle();

    const node = useWorkflow.getState().nodes[0]!;
    expect(node.data.spec.config).toMatchObject({
      prompt: '{{ vars.start.out }}',
      max_tokens: 2048,
    });
    expect(result.current.notes.get('a')?.keys).toEqual(
      expect.arrayContaining(['prompt', 'max_tokens']),
    );
    // The note has to carry the values too, or it cannot tell later whether it
    // still describes the node.
    expect(assistNoteApplies(result.current.notes.get('a'), node.data.spec.config)).toBe(true);
  });

  it('stops claiming credit once the settings are undone', async () => {
    // The chip used to survive an undo, so a node with nothing on it still said
    // "AI filled in". A note that outlives its own change is worse than no note.
    const { result } = renderHook(() => ({
      assist: useAutoAssist(),
      notes: useAssistNotes(),
    }));

    act(() => {
      useWorkflow.getState().setGraph(doc('wf-1', [{ id: 'a', description: '旧' }]));
    });
    await settle();
    act(() => {
      useWorkflow.getState().updateNode('a', { description: '写成中文详细回答' });
    });
    await settle();

    const filled = useWorkflow.getState().nodes[0]!;
    expect(filled.data.spec.config).not.toEqual({});
    expect(assistNoteApplies(result.current.notes.get('a'), filled.data.spec.config)).toBe(true);

    act(() => {
      useWorkflow.getState().undo();
    });

    const undone = useWorkflow.getState().nodes[0]!;
    expect(undone.data.spec.config).toEqual({});
    expect(assistNoteApplies(result.current.notes.get('a'), undone.data.spec.config)).toBe(false);
  });

  it('drops keys the user has since changed, and keeps the rest', async () => {
    const note = {
      keys: ['prompt', 'max_tokens'],
      values: { prompt: 'mine', max_tokens: 2048 },
    };
    expect(assistKeysInPlace(note, { prompt: 'mine', max_tokens: 2048 })).toEqual([
      'prompt',
      'max_tokens',
    ]);
    expect(assistKeysInPlace(note, { prompt: 'edited by hand', max_tokens: 2048 })).toEqual([
      'max_tokens',
    ]);
    expect(assistKeysInPlace(note, {})).toEqual([]);
  });

  it('does nothing at all in on-request mode', async () => {
    // Set before anything is mounted: the mode is module state, and changing it
    // while a hook is live would be a store update from outside React.
    setAssistMode('manual');

    renderHook(() => useAutoAssist());
    act(() => {
      useWorkflow.getState().setGraph(doc('wf-1', [{ id: 'a', description: '' }]));
    });
    await settle();
    act(() => {
      useWorkflow.getState().updateNode('a', { description: '做点什么' });
    });
    await settle();

    expect(fillNode).not.toHaveBeenCalled();
  });
});
