/**
 * Version history: list, compare, restore.
 *
 * Versions are the saved definitions, not the AI conversation. A version from
 * the copilot carries `meta.ai_change` (request, brief, plan, diff, trial
 * result) so the list can answer *why* it exists; a manual save has no such
 * record and says so rather than showing an empty card.
 *
 * Comparison is semantic: positions are ignored. Dragging a card does not make
 * two definitions different in any way that matters to a reader.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { GitCompare, History, RotateCcw } from 'lucide-react';

import { useAccess } from '@/access/accessStore';
import { api, ApiError } from '@/api/client';
import { GeneratePreview } from '@/ai/GeneratePreview';
import { useLocale, useT } from '@/i18n';
import { useWorkflow } from '@/store/workflowStore';
import type {
  CopilotChangeRecord,
  WorkflowGraph,
  WorkflowVersionSummary,
} from '@/types/workflow';
import { Dialog } from '@/ui/Dialogs';
import { diffCount, diffGraphs, type VersionDiff } from '@/versions/versionDiff';

type CompareTarget = 'current' | number;

export function VersionHistoryDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const locale = useLocale((state) => state.locale);
  const graphId = useWorkflow((state) => state.graphId);
  const publicDemo = useAccess((state) => state.status.capabilities.public_demo);

  const [versions, setVersions] = useState<WorkflowVersionSummary[]>([]);
  const [graphs, setGraphs] = useState<Record<number, WorkflowGraph>>({});
  const [selected, setSelected] = useState<number | null>(null);
  const [compare, setCompare] = useState<CompareTarget>('current');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [empty, setEmpty] = useState(false);

  // The canvas at the moment the dialog opened. A modal version browser is a
  // snapshot comparison; following live edits underneath would make "compare
  // with current" mean something different on every click.
  const currentGraph = useMemo(() => useWorkflow.getState().toGraph(), []);
  // Code workflows are display-only in the public demo. Their history stays
  // readable, but restoring one would mutate the shared canvas (and the save
  // that follows would be refused server-side anyway).
  const readOnly = useMemo(
    () =>
      publicDemo && currentGraph.nodes.some((node) => node.type === 'code'),
    [publicDemo, currentGraph],
  );

  const loadGraph = useCallback(
    async (version: number) => {
      if (graphs[version]) return;
      try {
        const graph = await api.getWorkflowVersion(graphId, version);
        setGraphs((previous) => ({ ...previous, [version]: graph }));
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      }
    },
    [graphId, graphs],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .listWorkflowVersions(graphId)
      .then((response) => {
        if (cancelled) return;
        const items = response.items ?? [];
        setVersions(items);
        setEmpty(items.length === 0);
        if (items[0]) {
          setSelected(items[0].version);
          void loadGraph(items[0].version);
        }
      })
      .catch((caught) => {
        if (cancelled) return;
        if (caught instanceof ApiError && caught.status === 404) {
          setEmpty(true);
        } else {
          setError(caught instanceof Error ? caught.message : String(caught));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // `loadGraph` is intentionally excluded: it changes with the graph cache,
    // and re-running this effect on every fetched version would refetch the
    // list in a loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphId]);

  useEffect(() => {
    if (typeof compare === 'number') void loadGraph(compare);
  }, [compare, loadGraph]);

  const selectedSummary = versions.find((item) => item.version === selected) ?? null;
  const selectedGraph = selected !== null ? graphs[selected] ?? null : null;
  const compareGraph =
    compare === 'current' ? currentGraph : graphs[compare] ?? null;

  const diff = useMemo(
    () =>
      selectedGraph && compareGraph
        ? diffGraphs(compareGraph, selectedGraph)
        : null,
    [selectedGraph, compareGraph],
  );

  const previewChanged = useMemo(() => {
    if (!diff) return undefined;
    return {
      added_nodes: diff.addedNodes.map((node) => node.id),
      removed_nodes: diff.removedNodes.map((node) => node.id),
      updated_nodes: diff.updatedNodes.map((change) => change.id),
      added_edges: diff.addedEdges.map((edge) => edge.id),
      removed_edges: diff.removedEdges.map((edge) => edge.id),
      updated_edges: diff.updatedEdges.map((edge) => edge.id),
    };
  }, [diff]);

  const choose = (version: number) => {
    setSelected(version);
    void loadGraph(version);
  };

  const restore = () => {
    if (readOnly || !selectedGraph || selected === null) return;
    if (!window.confirm(t('version.restoreConfirm', { version: String(selected) }))) {
      return;
    }
    useWorkflow.getState().replaceGraph(selectedGraph);
    onClose();
  };

  return (
    <Dialog
      title={t('version.title')}
      onClose={onClose}
      wide
      icon={<History size={14} />}
      footer={
        <>
          <div className="spacer" />
          <button type="button" className="btn" onClick={onClose}>
            {t('common.close')}
          </button>
          <button
            type="button"
            className="btn btn--primary"
            disabled={!selectedGraph || readOnly}
            title={readOnly ? t('version.readOnly') : undefined}
            onClick={restore}
          >
            <RotateCcw size={11} /> {t('version.restore')}
          </button>
        </>
      }
    >
      {loading ? (
        <p className="version-empty">{t('version.loading')}</p>
      ) : error ? (
        <p className="version-empty version-empty--error">
          {t('version.error', { message: error })}
        </p>
      ) : empty ? (
        <p className="version-empty">{t('version.empty')}</p>
      ) : (
        <>
          {readOnly ? (
            <p className="version-readonly" role="status">
              {t('version.readOnlyNote')}
            </p>
          ) : null}
          <div className="version-layout">
          <aside className="version-list">
            {versions.map((item) => (
              <button
                key={item.version}
                type="button"
                className={`version-item${item.version === selected ? ' version-item--active' : ''}`}
                onClick={() => choose(item.version)}
              >
                <span className="version-item__top">
                  <span className="version-item__number">v{item.version}</span>
                  {item.meta?.ai_change ? (
                    <span className="version-item__ai">{t('version.ai')}</span>
                  ) : null}
                </span>
                <span className="version-item__time">
                  {formatTime(item.created_at, locale)}
                </span>
                <span className="version-item__counts">
                  {t('version.nodeCount', { count: item.node_count })} ·{' '}
                  {t('version.edgeCount', { count: item.edge_count })}
                </span>
              </button>
            ))}
          </aside>

          <section className="version-detail">
            {selectedSummary && selectedGraph && diff ? (
              <>
                <header className="version-detail__header">
                  <strong className="version-detail__version">v{selected}</strong>
                  <span className="version-detail__time">
                    {formatTime(selectedSummary.created_at, locale)}
                  </span>
                  <div className="spacer" />
                  <label className="version-compare">
                    <GitCompare size={11} />
                    <span>{t('version.compare')}</span>
                    <select
                      value={String(compare)}
                      onChange={(event) =>
                        setCompare(
                          event.target.value === 'current'
                            ? 'current'
                            : Number(event.target.value),
                        )
                      }
                    >
                      <option value="current">{t('version.current')}</option>
                      {versions
                        .filter((item) => item.version !== selected)
                        .map((item) => (
                          <option key={item.version} value={item.version}>
                            v{item.version}
                          </option>
                        ))}
                    </select>
                  </label>
                </header>

                {selectedSummary.meta?.ai_change ? (
                  <AiChangeCard
                    change={selectedSummary.meta.ai_change}
                    graph={selectedGraph}
                  />
                ) : (
                  <p className="version-manual">{t('version.manual')}</p>
                )}

                <DiffSummary
                  diff={diff}
                  target={compare === 'current' ? t('version.current') : `v${compare}`}
                />

                <div className="version-preview">
                  <GeneratePreview graph={selectedGraph} changed={previewChanged} />
                </div>
              </>
            ) : (
              <p className="version-empty">{t('version.loading')}</p>
            )}
          </section>
          </div>
        </>
      )}
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

function AiChangeCard({
  change,
  graph,
}: {
  change: CopilotChangeRecord;
  graph: WorkflowGraph;
}) {
  const t = useT();
  // A record written by an older build may be partial. The card should show
  // what it has rather than throwing on a missing list.
  const changes = change.changes ?? {
    added_nodes: [],
    removed_nodes: [],
    updated_nodes: [],
    added_edges: [],
    removed_edges: [],
    updated_edges: [],
  };

  // The diff card below compares the version with the *current canvas*, which
  // reads as "+0" the moment the version is what is on screen. These names are
  // the record of what the AI actually did, independent of that comparison.
  const label = (nodeId: string): string =>
    graph.nodes.find((node) => node.id === nodeId)?.label || nodeId;
  const groups: { key: string; title: string; ids: string[] }[] = [
    { key: 'added', title: t('version.added'), ids: changes.added_nodes },
    { key: 'updated', title: t('version.updated'), ids: changes.updated_nodes },
    { key: 'removed', title: t('version.removed'), ids: changes.removed_nodes },
  ];

  return (
    <section className="version-ai-card">
      <header className="version-ai-card__header">
        <span>{t('version.ai')}</span>
        <span className="version-mode">
          {change.mode === 'auto' ? t('copilot.modeAuto') : t('copilot.modeConfirm')}
        </span>
        <span className="version-ai-card__counts">
          {t('version.aiBreakdown', {
            addedNodes: String(changes.added_nodes.length),
            removedNodes: String(changes.removed_nodes.length),
            updatedNodes: String(changes.updated_nodes.length),
            addedEdges: String(changes.added_edges.length),
            removedEdges: String(changes.removed_edges.length),
            updatedEdges: String(changes.updated_edges.length),
          })}
        </span>
      </header>

      {groups
        .filter((group) => group.ids.length > 0)
        .map((group) => (
          <p key={group.key} className="version-ai-card__line version-ai-card__names">
            <span className="version-ai-card__label">{group.title}</span>
            {group.ids.map(label).join('、')}
          </p>
        ))}

      {change.request ? (
        <p className="version-ai-card__line">
          <span className="version-ai-card__label">{t('version.request')}</span>
          {change.request}
        </p>
      ) : null}
      {change.summary ? (
        <p className="version-ai-card__line">
          <span className="version-ai-card__label">{t('version.summary')}</span>
          {change.summary}
        </p>
      ) : null}
      {change.brief?.goal ? (
        <p className="version-ai-card__line">
          <span className="version-ai-card__label">{t('version.goal')}</span>
          {change.brief.goal}
        </p>
      ) : null}
      {change.test ? (
        <p className="version-ai-card__line">
          <span className="version-ai-card__label">{t('version.test')}</span>
          {change.test.ok ? t('copilot.testPassed') : t('copilot.testFailed')}
          {change.test.duration_ms != null ? ` · ${Math.round(change.test.duration_ms)}ms` : ''}
        </p>
      ) : null}
      {change.output_review ? (
        <p className="version-ai-card__line">
          <span className="version-ai-card__label">{t('version.output')}</span>
          {change.output_review.matches
            ? t('copilot.outputMatched')
            : t('copilot.outputMismatch')}
          {change.output_review.reason ? ` · ${change.output_review.reason}` : ''}
        </p>
      ) : null}
    </section>
  );
}

function DiffSummary({ diff, target }: { diff: VersionDiff; target: string }) {
  const t = useT();
  const counts = diffCount(diff);
  const changed =
    counts.nodes > 0 ||
    counts.edges > 0 ||
    diff.addedNodes.length > 0 ||
    diff.removedNodes.length > 0;

  return (
    <section className="version-diff">
      <header className="version-diff__header">
        <span>
          {t('version.against', { target })}{' '}
          {t('version.changedCounts', {
            added: String(diff.addedNodes.length),
            removed: String(diff.removedNodes.length),
            updated: String(diff.updatedNodes.length),
            addedEdges: String(diff.addedEdges.length),
            removedEdges: String(diff.removedEdges.length),
            updatedEdges: String(diff.updatedEdges.length),
          })}
        </span>
      </header>

      {!changed ? (
        <p className="version-diff__none">{t('version.noDiff')}</p>
      ) : (
        <div className="version-diff__columns">
          <DiffColumn
            title={t('version.added')}
            items={diff.addedNodes.map((node) => node.label || node.id)}
            tone="add"
          />
          <DiffColumn
            title={t('version.removed')}
            items={diff.removedNodes.map((node) => node.label || node.id)}
            tone="remove"
          />
          <DiffColumn
            title={t('version.updated')}
            items={diff.updatedNodes.map(
              (change) => `${change.label} (${change.fields.join(', ')})`,
            )}
            tone="update"
          />
          <DiffColumn
            title={t('version.edges')}
            items={[
              ...diff.addedEdges.map((edge) => `+ ${edge.label || edge.id}`),
              ...diff.removedEdges.map((edge) => `− ${edge.label || edge.id}`),
              ...diff.updatedEdges.map(
                (edge) => `~ ${edge.label} (${edge.fields.join(', ')})`,
              ),
            ]}
            tone="edge"
          />
        </div>
      )}
    </section>
  );
}

function DiffColumn({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[];
  tone: 'add' | 'remove' | 'update' | 'edge';
}) {
  return (
    <div className={`version-diff__column version-diff__column--${tone}`}>
      <span className="version-diff__title">
        {title} <span className="version-diff__count">{items.length}</span>
      </span>
      <ul>
        {items.slice(0, 20).map((item, index) => (
          <li key={`${item}-${index}`}>{item}</li>
        ))}
        {items.length > 20 ? <li>…</li> : null}
      </ul>
    </div>
  );
}

function formatTime(value: string, locale: 'en' | 'zh'): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}
