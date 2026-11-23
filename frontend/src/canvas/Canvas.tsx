/**
 * The canvas.
 *
 * Owns three things the rest of the editor depends on:
 *
 *  1. **Dynamic port resolution.** A Condition node gains an output handle per
 *     configured branch, so ports cannot be derived from the node type alone.
 *     They are fetched from the backend (the single source of truth) and merged
 *     back into node data whenever a node's config changes.
 *  2. **Drop handling.** Palette drags arrive as a native drop event; the
 *     position must be converted from screen to flow coordinates, which is the
 *     one React Flow detail that is easy to get subtly wrong.
 *  3. **Run overlay.** Node statuses and streaming text are read from the store
 *     and projected onto the nodes, so the canvas answers "what is happening"
 *     without a second view.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Background,
  BackgroundVariant,
  ConnectionLineType,
  Controls,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  SelectionMode,
  useReactFlow,
  useViewport,
  type NodeMouseHandler,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';

import { api, type ResolvedPorts } from '@/api/client';
import { assistNoteApplies, useAssistNotes } from '@/assist/auto';
import { useCopilot } from '@/copilot/copilotStore';
import { LoopBackEdge } from '@/canvas/LoopBackEdge';
import { loopProgressFromEvents, withLoopEdges } from '@/canvas/loopEdges';
import { acceptScopeChange, selectionFocus } from '@/canvas/selectionFocus';
import { BaseNode, portColor } from '@/canvas/nodes/BaseNode';
import { useT } from '@/i18n';
import {
  selectErrors,
  useWorkflow,
  type CanvasNode,
  type NodeOutputSummary,
  type NodeRunStatus,
} from '@/store/workflowStore';
import { Icon } from '@/ui/Icon';
import {
  AlertTriangle,
  BarChart3,
  FileText,
  Loader2,
  MousePointerSquareDashed,
  ShoppingCart,
  Sparkles,
} from 'lucide-react';

/** Registered node components. One component handles every type. */
const nodeTypes = { scaffold: BaseNode };

/**
 * Edges that need more than a path: the loop's return edge arcs clear of the
 * node row and carries the current pass number. Everything else stays on React
 * Flow's built-in `smoothstep`.
 */
const edgeTypes = { loopback: LoopBackEdge };

/** Reduce a step's outputs to a size a card can show. */
function summariseOutput(outputs: Record<string, unknown> | null | undefined): NodeOutputSummary {
  const keys = outputs ? Object.keys(outputs) : [];
  const only = keys.length === 1 ? keys[0]! : null;
  // A single string is the shape a terminal node produces: the answer itself.
  if (only !== null && typeof outputs?.[only] === 'string') {
    return { kind: 'text', size: String(outputs[only]).length };
  }
  if (keys.length === 0) return { kind: 'empty', size: 0 };
  return { kind: 'fields', size: keys.length };
}

/**
 * The three starting points offered on an empty canvas.
 *
 * Keys only — the title and the description are both read from the catalogue,
 * so a scenario is translated like any other string. The description is handed
 * to the generator as the prompt, which is why it reads as a description of a
 * workflow rather than as a label.
 */
const SCENARIOS = [
  { key: 'contract', Icon: FileText },
  { key: 'support', Icon: ShoppingCart },
  { key: 'research', Icon: BarChart3 },
] as const;

export function Canvas({
  onRepair,
  onScenario,
  onOpenRun,
}: {
  onRepair?: () => void;
  /** Called with a scenario's description when one is picked from the empty canvas. */
  onScenario?: (prompt: string) => void;
  /** Reveal the run panel — the run strip on the canvas links here. */
  onOpenRun?: () => void;
}) {
  return (
    <ReactFlowProvider>
      <CanvasInner onRepair={onRepair} onScenario={onScenario} onOpenRun={onOpenRun} />
    </ReactFlowProvider>
  );
}

function CanvasInner({
  onRepair,
  onScenario,
  onOpenRun,
}: {
  onRepair?: () => void;
  onScenario?: (prompt: string) => void;
  onOpenRun?: () => void;
}) {
  const wrapper = useRef<HTMLDivElement>(null);
  const { screenToFlowPosition, fitView } = useReactFlow();

  const nodes = useWorkflow((state) => state.nodes);
  const edges = useWorkflow((state) => state.edges);
  const nodeTypesCatalogue = useWorkflow((state) => state.nodeTypes);
  const portColors = useWorkflow((state) => state.portColors);
  // `issues` rather than `selectErrors(issues)`: the latter allocates a new
  // array per call, which useSyncExternalStore reads as a changed snapshot and
  // re-renders forever.
  const issues = useWorkflow((state) => state.issues);
  const stepByNode = useWorkflow((state) => state.stepByNode);
  const runStatus = useWorkflow((state) => state.runStatus);
  const runEvents = useWorkflow((state) => state.runEvents);
  const selectedNodeId = useWorkflow((state) => state.selectedNodeId);
  const selectedEdgeId = useWorkflow((state) => state.selectedEdgeId);
  const setCopilotSelection = useWorkflow((state) => state.setCopilotSelection);
  const copilotSelection = useWorkflow((state) => state.copilotSelection);
  const copilotMode = useCopilot((state) => state.mode);
  const viewport = useViewport();
  // Marquee selection is a copilot affordance: it turns drag into a box
  // selection instead of panning, so the user can point at a region and then
  // describe the change.
  const selectingScope = useCopilot((state) => state.selecting);

  const onNodesChange = useWorkflow((state) => state.onNodesChange);
  const onEdgesChange = useWorkflow((state) => state.onEdgesChange);
  const onConnect = useWorkflow((state) => state.onConnect);
  const isValidConnection = useWorkflow((state) => state.isValidConnection);
  const addNodeFromType = useWorkflow((state) => state.addNodeFromType);
  const selectNode = useWorkflow((state) => state.selectNode);
  const selectEdge = useWorkflow((state) => state.selectEdge);
  const setNodesInternal = useWorkflow.setState;
  const duplicateNode = useWorkflow((state) => state.duplicateNode);
  const removeNode = useWorkflow((state) => state.removeNode);
  const tidyLayout = useWorkflow((state) => state.tidyLayout);

  const [dropHint, setDropHint] = useState(false);
  // Right-click menu: at most one open, positioned in wrapper coordinates.
  const [menu, setMenu] = useState<{ x: number; y: number; nodeId?: string } | null>(null);

  const t = useT();
  const errors = useMemo(() => selectErrors(issues), [issues]);

  /** Errors per node, so the card itself can point at the problem. */
  const errorByNode = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const issue of errors) {
      if (!issue.node_id) continue;
      counts[issue.node_id] = (counts[issue.node_id] ?? 0) + 1;
    }
    return counts;
  }, [errors]);

  // -------------------------------------------------------------------------
  // Dynamic ports
  // -------------------------------------------------------------------------

  /**
   * A signature that changes only when a node's port layout could have changed.
   * Without it this effect would refetch on every keystroke in the Inspector.
   */
  const portSignature = useMemo(
    () =>
      nodes
        .map((node) => `${node.id}:${node.data.spec.type}:${JSON.stringify(node.data.spec.config)}`)
        .join('|'),
    [nodes],
  );

  useEffect(() => {
    let cancelled = false;

    const resolveAll = async () => {
      const needsResolution = nodes.filter(
        (node) => nodeTypesCatalogue[node.data.spec.type]?.dynamic_outputs === 'from_param',
      );
      if (needsResolution.length === 0) return;

      const results = await Promise.allSettled(
        needsResolution.map(async (node) => ({
          id: node.id,
          ports: await api.resolvePorts(node.data.spec),
        })),
      );

      if (cancelled) return;

      const updates = new Map<string, ResolvedPorts>();
      for (const result of results) {
        if (result.status === 'fulfilled') updates.set(result.value.id, result.value.ports);
      }
      if (updates.size === 0) return;

      setNodesInternal((state) => ({
        nodes: state.nodes.map((node) => {
          const ports = updates.get(node.id);
          if (!ports) return node;
          // Only replace when the handle set actually changed, to avoid a
          // render loop driven by new array identities.
          const same =
            node.data.outputs.length === ports.outputs.length &&
            node.data.outputs.every((port, index) => port.id === ports.outputs[index]?.id);
          if (same) return node;
          return {
            ...node,
            data: {
              ...node.data,
              outputs: ports.outputs,
              inputs: ports.inputs,
            },
          };
        }),
      }));
    };

    void resolveAll();
    return () => {
      cancelled = true;
    };
    // `portSignature` intentionally stands in for `nodes`: it is the cheap
    // proxy for "ports may differ".
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [portSignature, nodeTypesCatalogue, setNodesInternal]);

  // "Tidy layout" replaces every position at once. Without a reframe the
  // result can land outside the current viewport and look like nothing
  // happened, so the store asks for a fit after applying the new graph.
  useEffect(() => {
    const onFit = () => {
      void fitView({ padding: 0.22, duration: 220 });
    };
    window.addEventListener('scaffold:fit-view', onFit);
    return () => window.removeEventListener('scaffold:fit-view', onFit);
  }, [fitView]);

  // -------------------------------------------------------------------------
  // Run overlay: node status + streaming text
  // -------------------------------------------------------------------------

  const streamByNode = useMemo(() => {
    const map: Record<string, string> = {};
    for (const event of runEvents) {
      if (event.type !== 'node_output_chunk' || !event.node_id) continue;
      const text = event.data.text;
      if (typeof text === 'string') map[event.node_id] = text;
    }
    return map;
  }, [runEvents]);

  /**
   * Warning-level logs, per node.
   *
   * A node that reports a problem and returns anyway is the dangerous case: the
   * run finishes green, the answer looks finished, and the one line saying
   * "there was nothing to search" sits in a log nobody opens. A retriever with
   * no corpus is exactly this — it searches the data piped into it, finds it,
   * and reports success. Surfacing the warning on the card is the difference
   * between a run that looks correct and one that is.
   */
  const warningByNode = useMemo(() => {
    const map: Record<string, string[]> = {};
    for (const event of runEvents) {
      if (event.type !== 'log' || !event.node_id) continue;
      const level = event.data.level;
      if (level !== 'warning' && level !== 'error') continue;
      const message = event.data.message;
      if (typeof message === 'string' && message) (map[event.node_id] ??= []).push(message);
    }
    return map;
  }, [runEvents]);

  // What the assistant did on its own, so a node it filled in does not look
  // identical to one the user set up by hand — the whole point of the mode is
  // that it acts unasked, which only works if it is also visible afterwards.
  const assistNotes = useAssistNotes();

  // A run that is queued or running has nodes it has not reached yet, and saying
  // so is what turns the canvas from "what is happening" into "what is left".
  // With no run in flight there is nothing to say, so nothing is drawn.
  const runInFlight = runStatus === 'running' || runStatus === 'queued';

  /**
   * How far the run has got, and what it is on now.
   *
   * The run panel is a side tab, so a user who is editing parameters sees
   * nothing at all while their workflow runs. This is the same information, put
   * where the work is.
   */
  const runProgress = useMemo(() => {
    const steps = Object.values(stepByNode);
    return {
      done: steps.filter((step) => step.status !== 'running').length,
      total: nodes.length,
      current: steps.find((step) => step.status === 'running')?.node_label ?? '',
    };
  }, [stepByNode, nodes.length]);

  /**
   * What each node produced, at the altitude a card can carry: how much, not what.
   *
   * This is the closest the canvas can honestly get to showing data flow. An
   * edge does not carry data — by design, nodes write shared state and edges
   * only express order — so the flow cannot be drawn on the lines. It can be
   * shown on the cards, and an empty one is the case worth seeing: a step that
   * passed through nothing looks exactly like a step that worked.
   */
  const outputByNode = useMemo(() => {
    const map: Record<string, NodeOutputSummary> = {};
    for (const [nodeId, step] of Object.entries(stepByNode)) {
      if (step.status === 'running') continue;
      map[nodeId] = summariseOutput(step.outputs);
    }
    return map;
  }, [stepByNode]);

  /**
   * Which pass each Loop node is on.
   *
   * Read from the event history rather than `stepByNode`, because the latter is
   * overwritten every time the gate runs and would make the counter blink.
   */
  const loopProgress = useMemo(
    () => loopProgressFromEvents(runEvents, nodes),
    [runEvents, nodes],
  );

  const focus = useMemo(
    () => selectionFocus(edges, selectedNodeId, selectedEdgeId),
    [edges, selectedEdgeId, selectedNodeId],
  );

  const displayNodes = useMemo(() => {
    return nodes.map((node) => {
      const step = stepByNode[node.id];
      // `RunStep['status']` already is the vocabulary the node renders, so there
      // is nothing to translate here — the chain of ternaries that used to sit
      // in this spot only served to drop `skipped` on the floor, which made a
      // branch the run did not take look exactly like a node it never reached.
      const nodeRunStatus: NodeRunStatus | undefined = step
        ? step.status
        : runInFlight
          ? 'pending'
          : undefined;

      // Skip the clone when nothing changed — this runs on every event.
      const warnings = warningByNode[node.id];
      const output = outputByNode[node.id];
      // Only while it is still true of this node. An undone fill leaves a note
      // claiming credit for settings that are no longer there.
      const recorded = assistNotes.get(node.id);
      const assist = assistNoteApplies(recorded, node.data.spec.config ?? {})
        ? recorded
        : undefined;
      const loopState = loopProgress[node.id];
      const errorCount = errorByNode[node.id] ?? 0;

      // Edge selection lights the two cards it joins; node selection lights the
      // cards on the other end of its edges with a second colour. The selected
      // node itself keeps React Flow's own `selected` treatment.
      const relation =
        focus.kind === 'edge' && focus.primaryNodes.has(node.id)
          ? 'related'
          : focus.kind === 'node' && focus.secondaryNodes.has(node.id)
            ? 'neighbor'
            : undefined;
      // Everything outside the focus fades, so the glow is unambiguous even at
      // a zoom level where a 1px border tint would disappear.
      const dimmed =
        focus.kind !== null &&
        !relation &&
        node.id !== selectedNodeId;
      const className = [
        node.className,
        relation ? `node-relation--${relation}` : '',
        dimmed ? 'node--dimmed' : '',
      ]
        .filter(Boolean)
        .join(' ') || undefined;

      if (
        node.data.runStatus === nodeRunStatus &&
        node.data.streamText === streamByNode[node.id] &&
        node.data.runWarnings === warnings &&
        node.data.runOutput === output &&
        node.data.assistNote === assist &&
        node.data.loopProgress === loopState &&
        node.data.errorCount === errorCount &&
        node.className === className
      ) {
        return node;
      }
      return {
        ...node,
        className,
        data: {
          ...node.data,
          runStatus: nodeRunStatus,
          streamText: streamByNode[node.id],
          runWarnings: warnings,
          runOutput: output,
          assistNote: assist,
          loopProgress: loopState,
          errorCount,
        },
      };
    });
  }, [
    nodes,
    stepByNode,
    streamByNode,
    runInFlight,
    warningByNode,
    outputByNode,
    assistNotes,
    loopProgress,
    focus,
    selectedNodeId,
    errorByNode,
  ]);

  /**
   * Edges that are carrying work right now.
   *
   * React Flow already ships the animation (`animated` moves the dash along the
   * path); this only decides which edges get it and adds the run colour. Kept
   * out of the store because it is a property of *this* render, not of the
   * document — persisting it would mark the workflow dirty on every event.
   */
  const displayEdges = useMemo(() => {
    // Decorate first: the return edge needs a different type and geometry, and
    // only after that is it meaningful to ask whether it is carrying work.
    const decorated = withLoopEdges(edges, nodes, loopProgress);
    return decorated.map((edge) => {
      const loopState = loopProgress[edge.target];
      // A loop's return edge stays visibly in motion for the whole body pass,
      // not only during the gate's own (very short) execution. That continuous
      // flow is what makes "it went round again" visible without reading logs.
      const flowing =
        stepByNode[edge.source]?.status === 'running' ||
        stepByNode[edge.target]?.status === 'running' ||
        (edge.type === 'loopback' && loopState?.continuing === true);
      const incident = focus.incidentEdges.has(edge.id);
      const dimmed =
        focus.kind !== null && !incident && edge.id !== selectedEdgeId;
      if (!flowing && !incident && !dimmed) return edge;
      return {
        ...edge,
        animated: flowing,
        className: [
          edge.className,
          incident ? 'edge--incident' : '',
          flowing ? 'edge--flowing' : '',
          dimmed ? 'edge--dimmed' : '',
        ]
          .filter(Boolean)
          .join(' '),
      };
    });
  }, [edges, nodes, stepByNode, loopProgress, focus, selectedEdgeId]);

  // -------------------------------------------------------------------------
  // Interaction
  // -------------------------------------------------------------------------

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      setDropHint(false);

      const type = event.dataTransfer.getData('application/scaffold-node');
      if (!type) return;

      // The drop position is in screen space; React Flow needs flow space. This
      // conversion is what makes a node land under the cursor at any zoom.
      const position = screenToFlowPosition({ x: event.clientX, y: event.clientY });
      addNodeFromType(type, position);
    },
    [addNodeFromType, screenToFlowPosition],
  );

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
    setDropHint(true);
  }, []);

  const onNodeClick: NodeMouseHandler<CanvasNode> = useCallback(
    (event, node) => {
      selectNode(node.id);
      // A plain click means "show me this node", so the inspector opens. A
      // Shift-click is scope selection for the assistant, and stealing the
      // panel there would interrupt the sentence being written.
      if (!event.shiftKey) {
        window.dispatchEvent(
          new CustomEvent('scaffold:select-node', { detail: { id: node.id } }),
        );
      }
    },
    [selectNode],
  );

  const openMenuAt = useCallback(
    (clientX: number, clientY: number, nodeId?: string) => {
      const bounds = wrapper.current?.getBoundingClientRect();
      setMenu({
        x: clientX - (bounds?.left ?? 0),
        y: clientY - (bounds?.top ?? 0),
        nodeId,
      });
    },
    [],
  );

  const onNodeContextMenu: NodeMouseHandler<CanvasNode> = useCallback(
    (event, node) => {
      event.preventDefault();
      selectNode(node.id);
      openMenuAt(event.clientX, event.clientY, node.id);
    },
    [openMenuAt, selectNode],
  );

  // Clicking anywhere else (or Escape) dismisses the context menu. The menu
  // itself stops mousedown so its own items stay clickable.
  useEffect(() => {
    if (!menu) return;
    const close = () => setMenu(null);
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('mousedown', close);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('mousedown', close);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, [menu]);

  // A translucent frame around the copilot's write scope. In confirm mode the
  // chip says how many nodes are selected, but a number does not answer "which
  // ones?" at a glance — the frame does. It is an overlay in screen space, so
  // it follows pan/zoom without re-rendering React Flow's node layer.
  const scopeFrame = useMemo(() => {
    if (copilotMode !== 'confirm' || copilotSelection.nodes.length === 0) return null;
    const selected = new Set(copilotSelection.nodes);
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    for (const node of nodes) {
      if (!selected.has(node.id)) continue;
      const width = node.measured?.width ?? 232;
      const height = node.measured?.height ?? 90;
      minX = Math.min(minX, node.position.x);
      minY = Math.min(minY, node.position.y);
      maxX = Math.max(maxX, node.position.x + width);
      maxY = Math.max(maxY, node.position.y + height);
    }
    if (!Number.isFinite(minX)) return null;
    const pad = 14;
    return {
      left: viewport.x + (minX - pad) * viewport.zoom,
      top: viewport.y + (minY - pad) * viewport.zoom,
      width: (maxX - minX + pad * 2) * viewport.zoom,
      height: (maxY - minY + pad * 2) * viewport.zoom,
    };
  }, [copilotMode, copilotSelection.nodes, nodes, viewport.x, viewport.y, viewport.zoom]);

  const isEmpty = displayNodes.length === 0;

  return (
    <div
      ref={wrapper}
      className={`canvas ${dropHint ? 'canvas--dropping' : ''}`}
      onDrop={onDrop}
      onDragOver={onDragOver}
      onDragLeave={() => setDropHint(false)}
    >
      <ReactFlow
        nodes={displayNodes}
        edges={displayEdges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        onNodeContextMenu={onNodeContextMenu}
        onPaneContextMenu={(event) => {
          event.preventDefault();
          openMenuAt(event.clientX, event.clientY);
        }}
        onEdgeClick={(_event, edge) => selectEdge(edge.id)}
        onPaneClick={() => {
          // Clicking empty canvas is the explicit "clear the scope" gesture.
          // React Flow also fires an empty selection change when a live graph
          // snapshot replaces its nodes; that one is not a user gesture and is
          // filtered below.
          selectNode(null);
          setCopilotSelection([], []);
          setMenu(null);
        }}
        onSelectionChange={(params) => {
          const next = {
            nodes: params.nodes.map((node) => node.id),
            edges: params.edges.map((edge) => edge.id),
          };
          if (!acceptScopeChange(next, useWorkflow.getState().copilotSelection)) return;
          setCopilotSelection(next.nodes, next.edges);
        }}
        selectionOnDrag={selectingScope}
        panOnDrag={!selectingScope}
        selectionMode={SelectionMode.Partial}
        isValidConnection={isValidConnection}
        connectionLineType={ConnectionLineType.SmoothStep}
        defaultEdgeOptions={{ type: 'smoothstep', animated: false }}
        proOptions={{ hideAttribution: true }}
        fitView
        fitViewOptions={{ padding: 0.3, maxZoom: 1 }}
        minZoom={0.15}
        maxZoom={2.5}
        deleteKeyCode={['Delete', 'Backspace']}
        multiSelectionKeyCode="Shift"
        // Nodes are the only interactive surface; a lazy connection radius makes
        // small handles easier to hit without mis-drops.
        connectionRadius={28}
        elevateNodesOnSelect
      >
        {/* Keep the grid visible on the dark canvas; the earlier near-black
            values disappeared on lower-contrast screens. */}
        <Background variant={BackgroundVariant.Dots} gap={18} size={1.5} color="#94a3b8" />
        <Controls showInteractive={false} position="bottom-left" />
        {/* An empty minimap is a large empty panel sitting on top of the welcome
            card; there is nothing to navigate before the first node exists. */}
        {isEmpty ? null : (
          <MiniMap
            position="bottom-left"
            pannable
            zoomable
            nodeColor={(node) => (node.data as CanvasNode['data']).color ?? '#475569'}
            nodeStrokeWidth={2}
            maskColor="rgba(8, 11, 17, 0.75)"
            style={{ background: 'var(--bg-panel)' }}
          />
        )}

        {isEmpty ? (
          <Panel position="top-center" className="canvas__welcome">
            <MousePointerSquareDashed size={22} />
            <strong>{t('canvas.empty.title')}</strong>
            <span>{t('canvas.empty.hint')}</span>

            {/* A blank canvas is a question with no answer available to someone
                who has not used this before. Three named scenarios turn the
                first move from "invent a workflow" into "pick one". */}
            {onScenario ? (
              <>
                <span className="canvas__welcome-try">{t('canvas.empty.try')}</span>
                <div className="canvas__scenarios">
                  {SCENARIOS.map(({ key, Icon: ScenarioIcon }) => (
                    <button
                      key={key}
                      type="button"
                      className="scenario-card"
                      onClick={() => onScenario(t(`scenario.${key}.prompt`))}
                    >
                      <ScenarioIcon size={15} className="scenario-card__icon" />
                      <span className="scenario-card__title">{t(`scenario.${key}.title`)}</span>
                      <span className="scenario-card__go">{t('scenario.try')}</span>
                    </button>
                  ))}
                </div>
              </>
            ) : null}
          </Panel>
        ) : null}

        {/* Bottom-centre rather than beside the issues panel: a run in progress
            is the thing the user is waiting on, and it should not have to
            compete with a validation warning for the same corner. */}
        {runInFlight ? (
          <Panel position="bottom-center" className="canvas__run">
            <Loader2 size={12} className="spin" />
            <span>
              {t('canvas.run.progress', {
                done: String(runProgress.done),
                total: String(runProgress.total),
              })}
            </span>
            {runProgress.current ? (
              <span className="canvas__run-current">
                {t('canvas.run.now', { label: runProgress.current })}
              </span>
            ) : null}
            {onOpenRun ? (
              <button type="button" className="canvas__run-link" onClick={onOpenRun}>
                {t('canvas.run.details')}
              </button>
            ) : null}
          </Panel>
        ) : null}

        {errors.length > 0 ? (
          <Panel position="top-right" className="canvas__issues">
            <AlertTriangle size={12} />
            <span>
              {t(errors.length === 1 ? 'canvas.issues.one' : 'canvas.issues.many', {
                count: errors.length,
              })}
            </span>
            {errors[0]?.node_id ? (
              <button
                type="button"
                className="canvas__issues-jump"
                onClick={() => {
                  // Select *and* reveal: the button says "show", and a
                  // selection behind an open AI tab shows nothing.
                  window.dispatchEvent(
                    new CustomEvent('scaffold:select-node', {
                      detail: { id: errors[0]!.node_id! },
                    }),
                  );
                }}
              >
                {t('canvas.show')}
              </button>
            ) : null}
            {/*
              The fix affordance lives here, next to the problems, rather than in
              the header. It is only useful in the moment the user is looking at
              what is wrong, and it disappears with the problems themselves.
            */}
            {onRepair ? (
              <button
                type="button"
                className="canvas__issues-jump canvas__issues-fix"
                title={t('canvas.issues.fixHint')}
                onClick={onRepair}
              >
                <Sparkles size={11} />
                {t('canvas.issues.fix')}
              </button>
            ) : null}
          </Panel>
        ) : null}
      </ReactFlow>

      {menu ? (
        <div
          className="canvas__menu"
          role="menu"
          style={{ left: menu.x, top: menu.y }}
          onMouseDown={(event) => event.stopPropagation()}
        >
          {menu.nodeId ? (
            <>
              <button
                type="button"
                role="menuitem"
                className="canvas__menu-item"
                onClick={() => {
                  window.dispatchEvent(
                    new CustomEvent('scaffold:select-node', { detail: { id: menu.nodeId } }),
                  );
                  setMenu(null);
                }}
              >
                {t('canvas.menu.openNode')}
              </button>
              <button
                type="button"
                role="menuitem"
                className="canvas__menu-item"
                onClick={() => {
                  duplicateNode(menu.nodeId!);
                  setMenu(null);
                }}
              >
                {t('canvas.menu.duplicate')}
              </button>
              <button
                type="button"
                role="menuitem"
                className="canvas__menu-item canvas__menu-item--danger"
                onClick={() => {
                  removeNode(menu.nodeId!);
                  setMenu(null);
                }}
              >
                {t('canvas.menu.delete')}
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                role="menuitem"
                className="canvas__menu-item"
                onClick={() => {
                  void tidyLayout(6);
                  setMenu(null);
                }}
              >
                {t('canvas.menu.tidyCompact')}
              </button>
              <button
                type="button"
                role="menuitem"
                className="canvas__menu-item"
                onClick={() => {
                  void tidyLayout(0);
                  setMenu(null);
                }}
              >
                {t('canvas.menu.tidyLine')}
              </button>
            </>
          )}
        </div>
      ) : null}

      {scopeFrame ? (
        <div
          className="copilot-scope-region"
          style={scopeFrame}
          aria-hidden
          data-testid="copilot-scope-region"
        >
          <span className="copilot-scope-region__label">{t('copilot.scopeRegion')}</span>
        </div>
      ) : null}

      {/* Port colour legend — the one place that explains the palette. */}
      {!isEmpty && Object.keys(portColors).length > 0 ? (
        <PortLegend colors={portColors} />
      ) : null}
    </div>
  );
}

function PortLegend({ colors }: { colors: Record<string, string> }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const entries = Object.entries(colors).slice(0, 13);

  return (
    <div className={`legend ${open ? 'legend--open' : ''}`}>
      <button
        type="button"
        className="legend__toggle"
        onClick={() => setOpen((value) => !value)}
        title={t('canvas.legend.show')}
      >
        <Icon name="braces" size={12} />
        {open ? t('canvas.legend.hide') : t('canvas.legend.show')}
      </button>
      {open ? (
        <ul className="legend__list">
          {entries.map(([name, color]) => (
            <li key={name}>
              <span className="legend__dot" style={{ background: color }} />
              {name.replace(/_/g, ' ')}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export { portColor };
