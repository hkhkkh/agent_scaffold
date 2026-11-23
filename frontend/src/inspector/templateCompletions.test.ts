/**
 * Completion candidates are a pure function of the graph, so the rules that
 * matter — the Loop's top-level `iteration`, the current node's exclusion,
 * last-run fields — are tested without a browser.
 */

import { describe, expect, it } from 'vitest';
import type { Edge } from '@xyflow/react';

import type { CanvasNode } from '@/store/workflowStore';
import type { NodeTypeSpec, OutputPort } from '@/types/workflow';
import {
  buildTemplateCandidates,
  filterTemplateCandidates,
} from '@/inspector/templateCompletions';

const port = (id: string, type = 'any'): OutputPort =>
  ({ id, label: id, type }) as OutputPort;

const node = (
  id: string,
  type: string,
  outputs: OutputPort[] = [],
  config: Record<string, unknown> = {},
): CanvasNode =>
  ({ id, data: { spec: { type, config }, outputs } }) as unknown as CanvasNode;

const edge = (source: string, target: string, sourceHandle?: string): Edge =>
  ({ id: `${source}-${target}`, source, target, sourceHandle }) as Edge;

const nodeTypes = {
  start: { outputs: [port('out')], params: [] },
  loop: { outputs: [], params: [] },
  code: {
    outputs: [port('out'), port('stdout', 'text')],
    params: [{ name: 'code' }, { name: 'fail_on_error' }],
  },
  tool: {
    outputs: [port('out', 'json'), port('text', 'text')],
    params: [{ name: 'tool' }, { name: 'arguments' }],
  },
  end: { outputs: [], params: [] },
} as unknown as Record<string, NodeTypeSpec>;

const nodes = [
  node('start', 'start', [port('out')], { input_schema: { topics: [], max_pages: 3 } }),
  node('loop', 'loop'),
  node('plan', 'code'),
  node('search', 'tool'),
  node('rows', 'code'),
  node('end', 'end'),
];

const edges = [
  edge('start', 'loop'),
  edge('loop', 'plan', 'continue'),
  edge('plan', 'search'),
  edge('search', 'rows'),
  edge('rows', 'loop'),
  edge('loop', 'end', 'done'),
];

const paths = (candidates: ReturnType<typeof buildTemplateCandidates>): string[] =>
  candidates.map((candidate) => candidate.path);

describe('buildTemplateCandidates', () => {
  it('offers run inputs, this node’s params, and upstream outputs', () => {
    const candidates = buildTemplateCandidates({
      nodes,
      edges,
      nodeTypes,
      currentNodeId: 'rows',
    });
    const all = paths(candidates);

    expect(all).toContain('inputs.topics');
    expect(all).toContain('inputs.max_pages');
    expect(all).toContain('params.code');
    expect(all).toContain('params.fail_on_error');
    expect(all).toContain('vars.plan.out');
    expect(all).toContain('vars.plan.out.out');
    expect(all).toContain('vars.plan.out.stdout');
    expect(all).toContain('vars.search.out');
    expect(all).toContain('vars.search.out.text');
  });

  it('never offers the current node’s own output', () => {
    const all = paths(
      buildTemplateCandidates({ nodes, edges, nodeTypes, currentNodeId: 'rows' }),
    );
    expect(all.some((path) => path.startsWith('vars.rows.'))).toBe(false);
  });

  it('offers the Loop iteration only inside that loop’s body', () => {
    const inside = paths(
      buildTemplateCandidates({ nodes, edges, nodeTypes, currentNodeId: 'rows' }),
    );
    expect(inside).toContain('vars.loop.iteration');

    const outside = paths(
      buildTemplateCandidates({ nodes, edges, nodeTypes, currentNodeId: 'end' }),
    );
    expect(outside).not.toContain('vars.loop.iteration');
    // The Loop's `out` is the value it forwards, not a result object.
    expect(outside).not.toContain('vars.loop.out');
  });

  it('uses fields observed in the last run, which the port spec cannot know', () => {
    const all = paths(
      buildTemplateCandidates({
        nodes,
        edges,
        nodeTypes,
        currentNodeId: 'rows',
        observedOutputs: {
          search: { out: { results: [], engine: 'duckduckgo' } },
        },
      }),
    );

    expect(all).toContain('vars.search.out.results');
    expect(all).toContain('vars.search.out.engine');
  });

  it('prefers resolved dynamic ports over the type’s static list', () => {
    const dynamic = [
      ...nodes,
      node('cond', 'condition', [port('yes', 'boolean'), port('no', 'boolean')]),
    ];
    const all = paths(
      buildTemplateCandidates({
        nodes: dynamic,
        edges,
        nodeTypes,
        currentNodeId: 'rows',
      }),
    );

    expect(all).toContain('vars.cond.out.yes');
    expect(all).toContain('vars.cond.out.no');
  });
});

describe('filterTemplateCandidates', () => {
  const candidates = [
    { path: 'vars.plan.out.query', group: 'plan', detail: 'text' },
    { path: 'vars.plan.out', group: 'plan', detail: 'object' },
    { path: 'vars.search.out.results', group: 'search', detail: 'json' },
    { path: 'inputs.topics', group: 'inputs', detail: 'run input' },
  ];

  it('filters by the typed prefix', () => {
    expect(paths(filterTemplateCandidates(candidates, 'vars.pl'))).toEqual([
      'vars.plan.out.query',
      'vars.plan.out',
    ]);
  });

  it('is case-insensitive and caps the menu', () => {
    expect(filterTemplateCandidates(candidates, 'VARS.P', 1)).toHaveLength(1);
  });

  it('returns everything (up to the cap) for an empty prefix', () => {
    expect(filterTemplateCandidates(candidates, '')).toHaveLength(4);
  });
});
