/**
 * What a selection illuminates on the canvas.
 *
 * Kept out of `Canvas.tsx` as a pure function for the same reason the edge
 * decorators are: "which cards glow" is a rule with cases, and cases deserve
 * tests rather than a component you have to mount React Flow to check.
 *
 * Two gestures, two answers:
 *
 * * **An edge is selected** — the user asked "what does this connect?", so its
 *   two endpoint cards glow (primary), and the edge itself is included.
 * * **A node is selected** — the user asked "what does this touch?", so every
 *   edge attached to it is marked incident, and every card on the *other* end
 *   of those edges becomes a secondary neighbour.
 *
 * Selection is exclusive in the store (selecting one clears the other), so
 * exactly one branch runs.
 */

import type { Edge } from '@xyflow/react';

export type FocusKind = 'edge' | 'node' | null;

export interface SelectionFocus {
  kind: FocusKind;
  /** Nodes lit with the primary colour (an edge's endpoints). */
  primaryNodes: Set<string>;
  /** Nodes lit with the secondary colour (a selected node's one-hop neighbours). */
  secondaryNodes: Set<string>;
  /** Edges attached to the selection. */
  incidentEdges: Set<string>;
}

export function selectionFocus(
  edges: Edge[],
  selectedNodeId: string | null,
  selectedEdgeId: string | null,
): SelectionFocus {
  const primaryNodes = new Set<string>();
  const secondaryNodes = new Set<string>();
  const incidentEdges = new Set<string>();
  let kind: FocusKind = null;

  if (selectedEdgeId) {
    const edge = edges.find((item) => item.id === selectedEdgeId);
    if (edge) {
      kind = 'edge';
      primaryNodes.add(edge.source);
      primaryNodes.add(edge.target);
      incidentEdges.add(edge.id);
    }
  } else if (selectedNodeId) {
    kind = 'node';
    for (const edge of edges) {
      if (edge.source !== selectedNodeId && edge.target !== selectedNodeId) continue;
      incidentEdges.add(edge.id);
      const other: string =
        edge.source === selectedNodeId ? edge.target : edge.source;
      if (other !== selectedNodeId) secondaryNodes.add(other);
    }
  }

  return { kind, primaryNodes, secondaryNodes, incidentEdges };
}

/**
 * Whether an `onSelectionChange` payload is a real user gesture.
 *
 * React Flow fires an **empty** selection change when a live graph snapshot
 * replaces its nodes. That is not the user clearing the scope; accepting it
 * made the copilot's range chip say "nothing selected" after every AI edit.
 * The explicit clear gestures are clicking the pane and the clear button, and
 * both call `setCopilotSelection([], [])` directly.
 */
export function acceptScopeChange(
  next: { nodes: string[]; edges: string[] },
  current: { nodes: string[]; edges: string[] },
): boolean {
  if (next.nodes.length > 0 || next.edges.length > 0) return true;
  return current.nodes.length === 0 && current.edges.length === 0;
}
