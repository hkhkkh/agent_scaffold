/**
 * The return edge of a loop, drawn as an arc rather than a straight line.
 *
 * React Flow's `smoothstep` path for a right-to-left edge on a single-row
 * layout retraces the forward chain exactly: same y, same x range. The cycle is
 * then invisible — which is exactly what "我只能看到一条线" described. This edge
 * takes an orthogonal detour below both nodes: horizontal, vertical, horizontal,
 * vertical, horizontal, with the same rounded corners as every other edge. It
 * carries the current pass number when a run is live.
 */

import { BaseEdge, EdgeLabelRenderer, type EdgeProps } from '@xyflow/react';
import { RotateCcw } from 'lucide-react';

import { useT } from '@/i18n';
import type { LoopEdgeData } from '@/canvas/loopEdges';

export function LoopBackEdge({
  id,
  sourceX,
  sourceY,
  targetX,
  targetY,
  markerEnd,
  data,
  label,
  selected,
}: EdgeProps) {
  const t = useT();
  const loop = (data ?? {}) as LoopEdgeData;
  const iteration =
    typeof loop.loopIteration === 'number' ? loop.loopIteration : undefined;
  const max = typeof loop.loopMax === 'number' ? loop.loopMax : undefined;

  // Orthogonal routing, matching the rest of the canvas. The shape is a detour:
  // leave the source to the right, drop into the corridor BELOW EVERY NODE, run
  // back left, then rise into the target's left handle. Straight segments only —
  // the corners are rounded by the same small radius `smoothstep` uses.
  //
  // `detourY` comes from the renderer, which knows every node's bottom. Falling
  // back to a local dip keeps standalone renders (tests, a node mid-drag)
  // sensible without weakening the guarantee in the real canvas.
  const lead = 26;
  const interval = 96;
  const baseline =
    typeof loop.detourY === 'number'
      ? Math.max(loop.detourY, sourceY + interval, targetY + interval)
      : Math.max(sourceY, targetY) + interval;
  const rightX = sourceX + lead;
  const leftX = targetX - lead;
  const r = Math.min(10, Math.abs(rightX - leftX) / 2 - 1, interval / 2);

  const path =
    `M ${sourceX},${sourceY} ` +
    `L ${rightX - r},${sourceY} ` +
    `Q ${rightX},${sourceY} ${rightX},${sourceY + r} ` +
    `L ${rightX},${baseline - r} ` +
    `Q ${rightX},${baseline} ${rightX - r},${baseline} ` +
    `L ${leftX + r},${baseline} ` +
    `Q ${leftX},${baseline} ${leftX},${baseline - r} ` +
    `L ${leftX},${targetY + r} ` +
    `Q ${leftX},${targetY} ${leftX + r},${targetY} ` +
    `L ${targetX},${targetY}`;

  const labelX = (rightX + leftX) / 2;
  const labelY = baseline - 11;

  const rounds =
    iteration === undefined
      ? ''
      : max === undefined
        ? `${iteration}`
        : `${iteration}/${max}`;

  // Quiet by default. Clicking the edge (or selecting the Loop node) is the
  // reveal gesture: the purpose the AI wrote, plus the live pass number when a
  // run is inside this loop.
  const active = loop.loopContinuing === true;
  const caption =
    selected || active
      ? [label || t('edge.loop'), rounds].filter(Boolean).join(' · ')
      : null;

  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} />
      {caption ? (
        <EdgeLabelRenderer>
          <div
            className="loop-edge-label nodrag nopan"
            style={{
              transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`,
            }}
            title={caption}
          >
            <RotateCcw size={9} />
            {caption}
          </div>
        </EdgeLabelRenderer>
      ) : null}
    </>
  );
}
