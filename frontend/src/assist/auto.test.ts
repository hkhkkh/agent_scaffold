/**
 * The rules auto mode is built on.
 *
 * Auto mode changes the user's document without being asked, which is only
 * acceptable because of two promises: it never overwrites a value the user
 * typed, and everything it does lands as one ordinary undoable edit. Both are
 * invisible from the canvas and neither is expressible as a type, so they are
 * pinned here.
 *
 * The third group covers the mode preference itself, because a preference that
 * forgets itself on reload is how someone who needs auto mode ends up in manual
 * mode without ever knowing there was a choice.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { NodeTypeSpec, WorkflowGraph } from '@/types/workflow';

// The store validates on nearly every mutation, and none of this is about
// validation. Letting jsdom attempt real fetches would make the suite slow and
// flaky, so the client is stubbed — same reason as `workflowStore.test.ts`.
vi.mock('@/api/client', () => ({
  ApiError: class ApiError extends Error {},
  api: {
    validate: vi.fn(async () => ({ ok: true, issues: [] })),
    nodeTypes: vi.fn(async () => ({ types: [], port_colors: {} })),
    tools: vi.fn(async () => ({ tools: [] })),
    stateFields: vi.fn(async () => ({ reducers: [], type_labels: {} })),
    aiStatus: vi.fn(async () => ({ enabled: false })),
    savedWorkflows: vi.fn(async () => ({ workflows: [] })),
  },
}));

import { permitted } from '@/assist/auto';
import { getAssistMode, setAssistMode } from '@/assist/mode';
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

function graph(config: Record<string, unknown>): WorkflowGraph {
  return {
    id: 'wf-1',
    name: 'Workflow',
    description: '',
    version: 1,
    nodes: [
      {
        id: 'n1',
        type: 'llm',
        label: 'n1',
        description: '',
        position: { x: 0, y: 0 },
        config,
        ui: {},
        trusted: false,
      },
    ],
    edges: [],
    viewport: { x: 0, y: 0, zoom: 1 },
    state_schema: [],
  } as unknown as WorkflowGraph;
}

beforeEach(() => {
  useWorkflow.setState({
    nodeTypes: { llm: LLM_SPEC },
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

describe('permitted (the assistant never overwrites the user)', () => {
  it('writes a setting that is unset', () => {
    expect(permitted({}, { prompt: 'hello' }, undefined)).toEqual({ prompt: 'hello' });
  });

  it('refuses a setting the user filled in', () => {
    // The user wrote their own prompt. Nothing the assistant proposes for that
    // key may ever be written, however good it is.
    expect(permitted({ prompt: 'mine' }, { prompt: 'theirs' }, undefined)).toEqual({});
  });

  it('may update its own value while nobody has touched it', () => {
    const mine = new Map<string, unknown>([['prompt', 'first draft']]);
    expect(permitted({ prompt: 'first draft' }, { prompt: 'second draft' }, mine)).toEqual({
      prompt: 'second draft',
    });
  });

  it('stops updating a value the user edited after the assistant wrote it', () => {
    // It wrote this once; the value that is there now is not that one, so a
    // person changed it. From here on the key is the user's.
    const mine = new Map<string, unknown>([['prompt', 'first draft']]);
    expect(permitted({ prompt: 'edited by hand' }, { prompt: 'second draft' }, mine)).toEqual(
      {},
    );
  });

  it('keeps the writable keys and drops only the owned ones', () => {
    const mine = new Map<string, unknown>([['system_prompt', 'You are helpful.']]);
    expect(
      permitted(
        { prompt: 'mine', system_prompt: 'You are helpful.' },
        { prompt: 'theirs', system_prompt: 'You are terse.', temperature: 0.2 },
        mine,
      ),
    ).toEqual({ system_prompt: 'You are terse.', temperature: 0.2 });
  });
});

describe('applyConfig (everything the assistant does is one edit)', () => {
  it('applies every key and reports which ones changed', () => {
    useWorkflow.getState().setGraph(graph({}));
    const changed = useWorkflow
      .getState()
      .applyConfig('n1', { prompt: 'a', temperature: 0.2 });

    expect(changed.sort()).toEqual(['prompt', 'temperature']);
    const config = useWorkflow.getState().nodes[0]!.data.spec.config;
    expect(config).toEqual({ prompt: 'a', temperature: 0.2 });
  });

  it('is one undo, not one per key', () => {
    // The failure this guards: pressing Ctrl+Z after the assistant filled a node
    // should take the whole suggestion back, not peel it off field by field.
    useWorkflow.getState().setGraph(graph({}));
    useWorkflow.getState().applyConfig('n1', { prompt: 'a', temperature: 0.2 });

    useWorkflow.getState().undo();

    expect(useWorkflow.getState().nodes[0]!.data.spec.config).toEqual({});
  });

  it('leaves a key it is not changing alone, and writes no history for it', () => {
    useWorkflow.getState().setGraph(graph({ prompt: 'a' }));
    const changed = useWorkflow.getState().applyConfig('n1', { prompt: 'a' });

    expect(changed).toEqual([]);
    expect(useWorkflow.getState().past).toHaveLength(0);
  });

  it('treats an empty value as unset rather than storing it', () => {
    // Same rule as `updateConfig`: an empty string would defeat the backend's
    // required-param check and let a declared default silently apply.
    useWorkflow.getState().setGraph(graph({ prompt: 'a' }));
    useWorkflow.getState().applyConfig('n1', { prompt: '' });

    expect(useWorkflow.getState().nodes[0]!.data.spec.config).toEqual({});
  });
});

describe('assist mode', () => {
  /**
   * The mode is read once, when the module loads, so a test that cares what was
   * read has to load it again. Re-importing rather than reaching into the module
   * is the point: it exercises the path a returning visitor actually takes.
   */
  const loadFresh = async () => {
    vi.resetModules();
    return import('@/assist/mode');
  };

  beforeEach(() => {
    window.localStorage.clear();
  });

  it('defaults to automatic, because the person who cannot recover is the one who needs it', async () => {
    const mode = await loadFresh();
    expect(mode.getAssistMode()).toBe(mode.DEFAULT_ASSIST_MODE);
    expect(mode.DEFAULT_ASSIST_MODE).toBe('auto');
  });

  it('reads back a saved choice on the next visit', async () => {
    window.localStorage.setItem('scaffold.assist', 'manual');
    const mode = await loadFresh();
    expect(mode.getAssistMode()).toBe('manual');
  });

  it('falls back to the default when storage holds something it does not understand', async () => {
    window.localStorage.setItem('scaffold.assist', 'sometimes');
    const mode = await loadFresh();
    expect(mode.getAssistMode()).toBe(mode.DEFAULT_ASSIST_MODE);
  });

  it('writes the choice through, since a preference that forgets is not one', () => {
    setAssistMode('manual');
    expect(window.localStorage.getItem('scaffold.assist')).toBe('manual');
    expect(getAssistMode()).toBe('manual');
  });

  it('keeps working when storage refuses to be written', async () => {
    // Private windows and blocked site data throw on `setItem`. The choice still
    // has to apply for this session — losing it on reload is the lesser failure.
    const mode = await loadFresh();
    const spy = vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => {
      throw new Error('quota');
    });
    expect(() => mode.setAssistMode('manual')).not.toThrow();
    expect(mode.getAssistMode()).toBe('manual');
    spy.mockRestore();
  });
});
