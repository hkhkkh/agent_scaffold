import { describe, expect, it } from 'vitest';
import type { Edge } from '@xyflow/react';

import { acceptScopeChange, selectionFocus } from '@/canvas/selectionFocus';

const edges: Edge[] = [
  { id: 'e1', source: 'a', target: 'b' },
  { id: 'e2', source: 'b', target: 'c' },
  { id: 'e3', source: 'd', target: 'b' },
];

describe('selection focus', () => {
  it('lights nothing when nothing is selected', () => {
    const focus = selectionFocus(edges, null, null);
    expect(focus.kind).toBeNull();
    expect(focus.primaryNodes.size).toBe(0);
    expect(focus.secondaryNodes.size).toBe(0);
    expect(focus.incidentEdges.size).toBe(0);
  });

  it('lights the two endpoint cards of a selected edge', () => {
    const focus = selectionFocus(edges, null, 'e1');
    expect(focus.kind).toBe('edge');
    expect([...focus.primaryNodes].sort()).toEqual(['a', 'b']);
    expect([...focus.incidentEdges]).toEqual(['e1']);
    expect(focus.secondaryNodes.size).toBe(0);
  });

  it('lights every edge of a selected node and its one-hop neighbours', () => {
    const focus = selectionFocus(edges, 'b', null);
    expect(focus.kind).toBe('node');
    expect([...focus.incidentEdges].sort()).toEqual(['e1', 'e2', 'e3']);
    // c and d are one hop away; a is also one hop (e1) — all three glow.
    expect([...focus.secondaryNodes].sort()).toEqual(['a', 'c', 'd']);
    // The selected node is not painted as a neighbour of itself.
    expect(focus.secondaryNodes.has('b')).toBe(false);
    expect(focus.primaryNodes.size).toBe(0);
  });

  it('does not treat a self-loop as a neighbour of itself', () => {
    const focus = selectionFocus(
      [{ id: 'loop', source: 'b', target: 'b' }],
      'b',
      null,
    );
    expect([...focus.incidentEdges]).toEqual(['loop']);
    expect(focus.secondaryNodes.size).toBe(0);
  });
});

describe('copilot scope changes', () => {
  it('accepts a non-empty selection', () => {
    expect(
      acceptScopeChange({ nodes: ['a'], edges: [] }, { nodes: [], edges: [] }),
    ).toBe(true);
  });

  it('ignores the empty change React Flow emits when a snapshot replaces nodes', () => {
    expect(
      acceptScopeChange({ nodes: [], edges: [] }, { nodes: ['a', 'b'], edges: [] }),
    ).toBe(false);
  });

  it('allows an empty change when there was no scope to lose', () => {
    expect(
      acceptScopeChange({ nodes: [], edges: [] }, { nodes: [], edges: [] }),
    ).toBe(true);
  });
});
