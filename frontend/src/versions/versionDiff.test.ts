import { describe, expect, it } from 'vitest';

import { diffCount, diffGraphs } from '@/versions/versionDiff';
import type { WorkflowGraph } from '@/types/workflow';

function graph(nodes: unknown[], edges: unknown[]): WorkflowGraph {
  return { nodes, edges } as unknown as WorkflowGraph;
}

function node(id: string, patch: Record<string, unknown> = {}) {
  return {
    id,
    type: 'prompt',
    label: id,
    description: '',
    position: { x: 0, y: 0 },
    config: {},
    ui: {},
    trusted: false,
    ...patch,
  };
}

describe('version diff', () => {
  it('reports added and removed nodes', () => {
    const before = graph([node('a')], []);
    const after = graph([node('a'), node('b')], []);

    const diff = diffGraphs(before, after);
    expect(diff.addedNodes.map((item) => item.id)).toEqual(['b']);
    expect(diff.removedNodes).toHaveLength(0);
  });

  it('ignores a pure position change', () => {
    const before = graph([node('a')], []);
    const after = graph([node('a', { position: { x: 500, y: 200 } })], []);

    expect(diffGraphs(before, after).updatedNodes).toHaveLength(0);
  });

  it('reports the fields that changed on a node', () => {
    const before = graph([node('a', { config: { template: 'one' } })], []);
    const after = graph(
      [node('a', { config: { template: 'two' }, label: 'renamed' })],
      [],
    );

    const diff = diffGraphs(before, after);
    expect(diff.updatedNodes).toHaveLength(1);
    expect(diff.updatedNodes[0]!.fields.sort()).toEqual(['config', 'label']);
  });

  it('reports edge additions and label changes', () => {
    const edge = {
      id: 'e1',
      source: 'a',
      sourceHandle: 'out',
      target: 'b',
      targetHandle: 'in',
      label: 'first',
    };
    const before = graph([node('a'), node('b')], [edge]);
    const after = graph(
      [node('a'), node('b')],
      [
        { ...edge, label: 'second' },
        {
          id: 'e2',
          source: 'b',
          sourceHandle: 'out',
          target: 'a',
          targetHandle: 'in',
          label: 'new',
        },
      ],
    );

    const diff = diffGraphs(before, after);
    expect(diff.addedEdges.map((item) => item.id)).toEqual(['e2']);
    expect(diff.updatedEdges[0]!.fields).toEqual(['label']);
    expect(diffCount(diff)).toEqual({ nodes: 0, edges: 2 });
  });
});
