/**
 * Semantic diff between two workflow definitions.
 *
 * Position is deliberately ignored: the canvas layout is not what a version
 * comparison is about. Two definitions that differ only by where a card sits
 * should read as "unchanged", otherwise every dragged node looks like a
 * material edit in the version list.
 */

import type { EdgeSpec, NodeSpec, WorkflowGraph } from '@/types/workflow';

export interface NodeChange {
  id: string;
  label: string;
  before?: NodeSpec;
  after?: NodeSpec;
  fields: string[];
}

export interface EdgeChange {
  id: string;
  label: string;
  before?: EdgeSpec;
  after?: EdgeSpec;
  fields: string[];
}

export interface VersionDiff {
  addedNodes: NodeSpec[];
  removedNodes: NodeSpec[];
  updatedNodes: NodeChange[];
  addedEdges: EdgeSpec[];
  removedEdges: EdgeSpec[];
  updatedEdges: EdgeChange[];
}

const NODE_FIELDS = ['type', 'label', 'description', 'config', 'trusted'] as const;
const EDGE_FIELDS = [
  'source',
  'sourceHandle',
  'target',
  'targetHandle',
  'label',
] as const;

export function diffGraphs(before: WorkflowGraph, after: WorkflowGraph): VersionDiff {
  const beforeNodes = new Map(before.nodes.map((node) => [node.id, node]));
  const afterNodes = new Map(after.nodes.map((node) => [node.id, node]));
  const beforeEdges = new Map(before.edges.map((edge) => [edge.id, edge]));
  const afterEdges = new Map(after.edges.map((edge) => [edge.id, edge]));

  const addedNodes = after.nodes.filter((node) => !beforeNodes.has(node.id));
  const removedNodes = before.nodes.filter((node) => !afterNodes.has(node.id));
  const updatedNodes: NodeChange[] = [];

  for (const node of after.nodes) {
    const old = beforeNodes.get(node.id);
    if (!old) continue;
    const fields = NODE_FIELDS.filter(
      (field) => stable(old[field]) !== stable(node[field]),
    );
    if (fields.length > 0) {
      updatedNodes.push({
        id: node.id,
        label: node.label || node.id,
        before: old,
        after: node,
        fields: [...fields],
      });
    }
  }

  const addedEdges = after.edges.filter((edge) => !beforeEdges.has(edge.id));
  const removedEdges = before.edges.filter((edge) => !afterEdges.has(edge.id));
  const updatedEdges: EdgeChange[] = [];

  for (const edge of after.edges) {
    const old = beforeEdges.get(edge.id);
    if (!old) continue;
    const fields = EDGE_FIELDS.filter(
      (field) => stable(old[field]) !== stable(edge[field]),
    );
    if (fields.length > 0) {
      updatedEdges.push({
        id: edge.id,
        label: edge.label || `${edge.source} → ${edge.target}`,
        before: old,
        after: edge,
        fields: [...fields],
      });
    }
  }

  return {
    addedNodes,
    removedNodes,
    updatedNodes,
    addedEdges,
    removedEdges,
    updatedEdges,
  };
}

export function diffCount(diff: VersionDiff): { nodes: number; edges: number } {
  return {
    nodes:
      diff.addedNodes.length + diff.removedNodes.length + diff.updatedNodes.length,
    edges:
      diff.addedEdges.length + diff.removedEdges.length + diff.updatedEdges.length,
  };
}

function stable(value: unknown): string {
  try {
    return JSON.stringify(value ?? null);
  } catch {
    return String(value);
  }
}
