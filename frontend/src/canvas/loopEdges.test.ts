import { describe, expect, it } from 'vitest';
import type { Edge } from '@xyflow/react';

import {
  isBackEdge,
  loopProgressFromEvents,
  withLoopEdges,
} from '@/canvas/loopEdges';
import { specToEdge, type CanvasNode } from '@/store/workflowStore';
import type { RunEvent } from '@/types/workflow';

function node(id: string, type: string, x: number, y = 0): CanvasNode {
  return {
    id,
    type: 'scaffold',
    position: { x, y },
    data: {
      spec: {
        id,
        type,
        label: id,
        description: '',
        position: { x, y },
        config: type === 'loop' ? { max_iterations: 7 } : {},
        ui: { parent_id: null, width: null, height: null, collapsed: false, note: '' },
        trusted: false,
      },
      inputs: [],
      outputs: [],
      color: '#f59e0b',
      icon: 'repeat',
    },
  };
}

function event(nodeId: string, outputs: Record<string, unknown>): RunEvent {
  return {
    type: 'node_finished',
    run_id: 'r',
    seq: 1,
    node_id: nodeId,
    node_label: nodeId,
    step: 1,
    timestamp: '',
    data: { outputs },
  };
}

describe('loop-back edges', () => {
  it('recognises the return into the gate, and nothing else', () => {
    const start = node('start', 'start', 0);
    const loop = node('loop', 'loop', 320);
    const plan = node('plan', 'code', 640);
    const write = node('write', 'tool', 1600);

    expect(isBackEdge(write, loop)).toBe(true);
    // Start -> Loop is the entry edge, even though it also targets the loop.
    expect(isBackEdge(start, loop)).toBe(false);
    // Loop --continue--> plan is a forward branch, not a return.
    expect(isBackEdge(loop, plan)).toBe(false);
  });

  it('does not paint serpentine body edges as returns', () => {
    // The 42-node workflow lays its body in rows that alternate direction, so
    // many ordinary edges run right-to-left. They are not returns: a cycle in
    // this engine can only close through the Loop gate.
    const start = node('start', 'start', 0);
    const loop = node('loop', 'loop', 320, 0);
    const bodyLeft = node('body_left', 'code', 320, 320);
    const bodyRight = node('body_right', 'code', 640, 320);

    expect(isBackEdge(bodyRight, bodyLeft)).toBe(false);
    expect(isBackEdge(bodyRight, loop)).toBe(true);
    expect(isBackEdge(start, loop)).toBe(false);
  });

  it('marks only the return edge and carries the pass number', () => {
    const start = node('start', 'start', 0);
    const loop = node('loop', 'loop', 320);
    const write = node('write', 'tool', 1600);
    const edges: Edge[] = [
      { id: 'e1', source: 'start', target: 'loop' },
      { id: 'e2', source: 'write', target: 'loop' },
    ];

    const decorated = withLoopEdges(edges, [start, loop, write], {
      loop: { iteration: 3, max: 7, continuing: true },
    });

    expect(decorated[0]!.type).toBeUndefined();
    expect(decorated[1]!.type).toBe('loopback');
    expect(decorated[1]!.data).toMatchObject({ loopIteration: 3, loopMax: 7 });
    expect(decorated[1]!.className).toContain('edge--loop-back');
    expect(decorated[1]!.markerEnd).toBeTruthy();
  });

  it('reads the latest completed pass, which step state cannot keep', () => {
    const loop = node('loop', 'loop', 320);
    const progress = loopProgressFromEvents(
      [
        event('loop', { iteration: 1, continue: true, __loop__: { max_iterations: 7 } }),
        event('loop', { iteration: 2, continue: true, __loop__: { max_iterations: 7 } }),
        event('loop', { iteration: 3, continue: false }),
      ],
      [loop],
    );

    expect(progress.loop).toEqual({ iteration: 3, max: 7, continuing: false });
  });
});

describe('edge handles from AI-generated graphs', () => {
  it('accepts the snake_case spelling Pydantic sends without by_alias', () => {
    // The API dumps models by field name. Reading only `sourceHandle` dropped
    // the handle, which is what made a loop's return look disconnected.
    const edge = specToEdge({
      id: 'e',
      source: 'write',
      target: 'loop',
      source_handle: 'out',
      target_handle: 'in',
    } as unknown as Parameters<typeof specToEdge>[0]);

    expect(edge.sourceHandle).toBe('out');
    expect(edge.targetHandle).toBe('in');
  });
});
