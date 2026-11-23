/**
 * Candidate paths for a `{{ }}` template, derived from the actual graph.
 *
 * The reason this exists: the template language is uniform except for the Loop
 * node, whose `iteration` lives at the top level (`vars.loop.iteration`) rather
 * than under the usual `out` (the Loop's `out` is the value it forwards). A typo
 * or a guessed path does not raise — it renders as an empty string and the run
 * fails much later with a symptom that points at the wrong node.
 *
 * Everything here is pure so the candidate list can be unit-tested without a
 * browser. The UI only renders what this returns.
 */

import type { Edge } from '@xyflow/react';

import type { CanvasNode } from '@/store/workflowStore';
import type { NodeTypeSpec, OutputPort } from '@/types/workflow';

export interface TemplateCandidate {
  /** What gets inserted inside `{{ ... }}`. */
  path: string;
  /** Where it comes from, e.g. "plan · Code" or "inputs". */
  group: string;
  /** Port type or a short note. */
  detail: string;
}

export interface TemplateCompletionInput {
  nodes: CanvasNode[];
  edges: Edge[];
  nodeTypes: Record<string, NodeTypeSpec>;
  currentNodeId: string;
  /**
   * Outputs observed in the last run, keyed by node id (`RunStep.outputs`).
   *
   * Code, Tool and Agent nodes declare a single `out` port, so their nested
   * fields are only knowable at authoring time if a run has already produced
   * them. When it has, those keys are offered — which is the difference between
   * "vars.search.out" and "vars.search.out.results".
   */
  observedOutputs?: Record<string, Record<string, unknown> | null>;
}

/** Which nodes are inside each Loop's body, keyed by the Loop's id. */
function loopBodies(
  nodes: CanvasNode[],
  edges: Edge[],
): Map<string, Set<string>> {
  const outgoing = new Map<string, Edge[]>();
  for (const edge of edges) {
    const list = outgoing.get(edge.source);
    if (list) list.push(edge);
    else outgoing.set(edge.source, [edge]);
  }

  const bodies = new Map<string, Set<string>>();
  for (const loop of nodes) {
    if (loop.data.spec.type !== 'loop') continue;
    const body = new Set<string>();
    const stack = edges
      .filter((edge) => edge.source === loop.id && edge.sourceHandle === 'continue')
      .map((edge) => edge.target);

    while (stack.length > 0) {
      const id = stack.pop()!;
      if (id === loop.id || body.has(id)) continue;
      body.add(id);
      for (const edge of outgoing.get(id) ?? []) stack.push(edge.target);
    }
    bodies.set(loop.id, body);
  }
  return bodies;
}

/** Output ports a node is known to have, preferring the resolved ones. */
function outputPorts(node: CanvasNode, spec: NodeTypeSpec | undefined): OutputPort[] {
  const resolved = node.data.outputs;
  if (resolved && resolved.length > 0) {
    return resolved as unknown as OutputPort[];
  }
  return spec?.outputs ?? [];
}

export function buildTemplateCandidates(input: TemplateCompletionInput): TemplateCandidate[] {
  const { nodes, edges, nodeTypes, currentNodeId, observedOutputs } = input;
  const byId = new Map<string, TemplateCandidate>();

  const add = (path: string, group: string, detail: string): void => {
    if (!path || byId.has(path)) return;
    byId.set(path, { path, group, detail });
  };

  const current = nodes.find((node) => node.id === currentNodeId);
  const currentType = current?.data.spec.type ?? '';
  const currentSpec = nodeTypes[currentType];

  // 1. Run inputs. These are the values the Start node accepted for this run.
  const start = nodes.find((node) => node.data.spec.type === 'start');
  const schema = start?.data.spec.config?.input_schema;
  if (schema && typeof schema === 'object' && !Array.isArray(schema)) {
    for (const key of Object.keys(schema as Record<string, unknown>)) {
      add(`inputs.${key}`, 'inputs', 'run input');
    }
  }

  // 2. This node's own config keys.
  for (const param of currentSpec?.params ?? []) {
    add(`params.${param.name}`, 'params', 'this node');
  }

  // 3. Loop pass counters, only for nodes that actually sit inside that loop.
  const bodies = loopBodies(nodes, edges);
  for (const [loopId, body] of bodies) {
    if (!body.has(currentNodeId)) continue;
    const loop = nodes.find((node) => node.id === loopId);
    add(
      `vars.${loopId}.iteration`,
      loop?.data.spec.label || loopId,
      'loop pass (top level)',
    );
  }

  // 4. Every other node's outputs.
  for (const node of nodes) {
    if (node.id === currentNodeId) continue;
    if (node.data.spec.type === 'start' || node.data.spec.type === 'end') continue;

    const group = `${node.data.spec.label || node.id} · ${node.data.spec.type}`;
    const spec = nodeTypes[node.data.spec.type];

    if (node.data.spec.type === 'loop') {
      // The Loop is handled above for its body; its `out` is the forwarded
      // value rather than a result object, so do not offer it as one.
      continue;
    }

    add(`vars.${node.id}.out`, group, 'whole output');

    for (const port of outputPorts(node, spec)) {
      add(`vars.${node.id}.out.${port.id}`, group, port.type);
    }

    const observed = observedOutputs?.[node.id];
    const out = observed && typeof observed === 'object' ? observed.out : undefined;
    if (out && typeof out === 'object' && !Array.isArray(out)) {
      for (const key of Object.keys(out as Record<string, unknown>)) {
        add(`vars.${node.id}.out.${key}`, group, 'seen in last run');
      }
    }
  }

  return [...byId.values()];
}

/** Filter by the text the user has typed after `{{`, capped for the menu. */
export function filterTemplateCandidates(
  candidates: TemplateCandidate[],
  prefix: string,
  limit = 12,
): TemplateCandidate[] {
  const query = prefix.trim().toLowerCase();
  const matching = query
    ? candidates.filter((candidate) => candidate.path.toLowerCase().startsWith(query))
    : candidates;
  return matching.slice(0, limit);
}
