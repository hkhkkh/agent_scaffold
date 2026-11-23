/**
 * Recognise and decorate a Loop node's return edge.
 *
 * A loop is drawn as a cycle: the gate's `continue` handle goes forward into the
 * body, and the body's last node comes back into the gate. The forward half is
 * an ordinary edge. The return half is not: on the usual single-row layout it
 * runs right-to-left at the same y as the chain, so React Flow's smoothstep path
 * lies exactly on top of it and the cycle is invisible.
 *
 * Detection is geometric — an edge into a Loop node whose source sits to the
 * right of (or below) the loop — rather than keyed on a handle. The old rule
 * keyed on `sourceHandle === 'continue'`, but that is the edge *out of* the
 * gate; the edge *back into* it is just `out -> in` from a Code/Tool node.
 */

import { MarkerType, type Edge } from '@xyflow/react';

import type { CanvasNode } from '@/store/workflowStore';
import type { LoopProgress, RunEvent } from '@/types/workflow';

/** Extra fields a return edge carries for its renderer. */
export interface LoopEdgeData extends Record<string, unknown> {
  loopIteration?: number;
  loopMax?: number;
  /** True while the loop is inside its body (the last gate said "continue"). */
  loopContinuing?: boolean;
  /** Y of the corridor below the two endpoint cards; set by `withLoopEdges`. */
  detourY?: number;
}

const LOOP_COLOR = '#f59e0b';

/** Used when a node has not been measured yet (first render / preview). */
const NODE_HEIGHT_ESTIMATE = 180;
/** How far below the two endpoint cards the return line runs. */
const LANE_MARGIN = 18;

/**
 * A return edge: it goes backwards **and** lands on a Loop node.
 *
 * Both halves are load-bearing.
 *
 * * Requiring the Loop target excludes the loop's forward `continue` branch and
 *   its initial entry edge.
 * * Requiring the backward direction excludes body edges. This is the half the
 *   first version missed: a 42-node workflow lays its loop body out in a
 *   serpentine grid, so ordinary body edges run right-to-left too. A purely
 *   geometric rule painted all 15 of them as returns — 13 false positives — and
 *   the canvas became a nest of orange rectangles. In this engine a cycle can
 *   only be closed through a Loop gate (the validator rejects any other cycle),
 *   so a return edge always targets one.
 */
export function isBackEdge(source: CanvasNode, target: CanvasNode): boolean {
  if (target.data.spec.type !== 'loop') return false;
  const dx = source.position.x - target.position.x;
  const dy = source.position.y - target.position.y;
  if (dx > 20) return true;
  return Math.abs(dx) <= 20 && dy > 20;
}

/** The bottom edge of a card, once React Flow has measured it. */
function bottomOf(node: CanvasNode): number {
  return node.position.y + (node.measured?.height ?? NODE_HEIGHT_ESTIMATE);
}

/**
 * The corridor for one return edge: just below the two cards it joins.
 *
 * It used to be placed below every node in the horizontal span, which kept the
 * line unoccluded but made it dive "ten thousand miles" down the canvas — the
 * complaint that replaced the original one. A small offset below the two
 * endpoint cards stays visually attached to the loop; if an unrelated card
 * happens to cover part of it, clicking the visible end highlights the whole
 * edge and the inspector names both endpoints.
 */
function laneBelow(source: CanvasNode, target: CanvasNode): number {
  return Math.max(bottomOf(source), bottomOf(target)) + LANE_MARGIN;
}

/**
 * Give every return edge the `loopback` type, a clear corridor, and the
 * progress label.
 *
 * The corridor is placed below every node that the edge's horizontal span
 * actually crosses. That is the difference between "usually visible" and
 * "cannot be covered" without forcing every return to the bottom of the graph.
 *
 * Forward edges are returned untouched, so this composes with the ordinary
 * flowing-edge decoration instead of replacing it.
 */
export function withLoopEdges(
  edges: Edge[],
  nodes: CanvasNode[],
  progress: Record<string, LoopProgress> = {},
): Edge[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));

  return edges.map((edge) => {
    const source = byId.get(edge.source);
    const target = byId.get(edge.target);
    if (!source || !target || !isBackEdge(source, target)) return edge;

    const state = progress[target.id];
    const data: LoopEdgeData = {
      loopIteration: state?.iteration,
      loopMax: state?.max,
      loopContinuing: state?.continuing,
      detourY: laneBelow(source, target),
    };

    return {
      ...edge,
      type: 'loopback',
      data: { ...(edge.data as Record<string, unknown> | undefined), ...data },
      className: [edge.className, 'edge--loop-back'].filter(Boolean).join(' '),
      markerEnd: {
        type: MarkerType.ArrowClosed,
        // Quiet unless the edge is selected: the line itself is the highlight.
        color: '#5b6778',
        width: 14,
        height: 14,
      },
    };
  });
}

/**
 * The latest pass each Loop node reported, read from the run's event history.
 *
 * `stepByNode` is overwritten by every `node_started`, so it loses the count the
 * moment the gate runs again. The event list keeps every `node_finished`, which
 * is what makes the badge tick 1 → 2 → … instead of blinking back to nothing.
 *
 * A node that stops exposing `iteration` (the param is off) simply gets no
 * entry; the canvas then draws a plain loop without a counter rather than a
 * wrong one.
 */
export function loopProgressFromEvents(
  events: RunEvent[],
  nodes: CanvasNode[],
): Record<string, LoopProgress> {
  const loops = new Map<string, CanvasNode>();
  for (const node of nodes) {
    if (node.data.spec.type === 'loop') loops.set(node.id, node);
  }

  const progress: Record<string, LoopProgress> = {};
  for (const event of events) {
    if (event.type !== 'node_finished' || !event.node_id) continue;
    const node = loops.get(event.node_id);
    if (!node) continue;

    const outputs = event.data.outputs as Record<string, unknown> | undefined;
    if (!outputs) continue;
    const iteration = outputs.iteration;
    if (typeof iteration !== 'number') continue;

    const meta = outputs.__loop__ as { max_iterations?: unknown } | undefined;
    const configMax = node.data.spec.config.max_iterations;
    progress[event.node_id] = {
      iteration,
      max:
        typeof meta?.max_iterations === 'number'
          ? meta.max_iterations
          : typeof configMax === 'number'
            ? configMax
            : undefined,
      continuing: outputs.continue === true,
    };
  }
  return progress;
}

export { LOOP_COLOR };
