/**
 * Read-only canvas of a workflow being generated.
 *
 * The generator streams a complete graph snapshot after every mutation; this
 * renders that snapshot with the same node component the real canvas uses, so
 * "what the AI drew" and "what you will get" are visually the same thing. It is
 * deliberately not editable: nothing here may change the graph, and the draft
 * still has to be accepted before it reaches the store.
 */

import { useEffect, useMemo } from 'react';
import {
  Background,
  BackgroundVariant,
  Controls,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { LoopBackEdge } from '@/canvas/LoopBackEdge';
import { withLoopEdges } from '@/canvas/loopEdges';
import { BaseNode } from '@/canvas/nodes/BaseNode';
import { specToEdge, specToNode, useWorkflow } from '@/store/workflowStore';
import type { GenerationEvent, WorkflowGraph } from '@/types/workflow';

const nodeTypes = { scaffold: BaseNode };
const edgeTypes = { loopback: LoopBackEdge };

export function GeneratePreview({
  graph,
  changed,
}: {
  graph: WorkflowGraph | null;
  changed?: GenerationEvent['changed'];
}) {
  const catalogue = useWorkflow((state) => state.nodeTypes);

  const nodes = useMemo(() => {
    if (!graph) return [];
    const fresh = new Set([
      ...(changed?.added_nodes ?? []),
      ...(changed?.updated_nodes ?? []),
    ]);
    return graph.nodes
      .filter((spec) => catalogue[spec.type])
      .map((spec) => {
        const node = specToNode(spec, catalogue[spec.type]!);
        return fresh.has(spec.id) ? { ...node, className: 'preview-node--new' } : node;
      });
  }, [graph, changed, catalogue]);

  // The draft has no run, so there is no pass counter — but the return edge is
  // still drawn as an arc. That is what lets a user see "this is a loop" while
  // watching the AI build it, instead of discovering it only at run time.
  const edges = useMemo(
    () => withLoopEdges(graph ? graph.edges.map(specToEdge) : [], nodes),
    [graph, nodes],
  );

  if (!graph || nodes.length === 0) {
    return <div className="generate-preview generate-preview--empty" />;
  }

  return (
    <div className="generate-preview">
      <ReactFlowProvider>
        <PreviewCanvas nodes={nodes} edges={edges} />
      </ReactFlowProvider>
    </div>
  );
}

function PreviewCanvas({
  nodes,
  edges,
}: {
  nodes: ReturnType<typeof specToNode>[];
  edges: ReturnType<typeof specToEdge>[];
}) {
  const { fitView } = useReactFlow();

  // Re-fit whenever the draft grows. Waiting a frame lets React Flow measure
  // the new nodes first; without it the first snapshot renders at the default
  // zoom and the canvas looks stuck.
  useEffect(() => {
    const handle = requestAnimationFrame(() => {
      void fitView({ padding: 0.18, duration: 220 });
    });
    return () => cancelAnimationFrame(handle);
  }, [nodes.length, edges.length, fitView]);

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      fitView
      minZoom={0.15}
      maxZoom={1.2}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
      panOnDrag
      zoomOnScroll={false}
      zoomOnDoubleClick={false}
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={18} size={1} />
      <Controls showInteractive={false} showZoom={false} />
    </ReactFlow>
  );
}
