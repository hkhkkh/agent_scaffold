/**
 * Saved workflow browser.
 *
 * Kept as a dialog rather than a persistent sidebar: opening a workflow is a
 * deliberate, occasional act, and the canvas deserves the horizontal space.
 *
 * The list is fetched on open rather than cached, because the store's saved
 * timestamp is per-document and a stale list would let a user "open" the version
 * they are already looking at.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import { AlertCircle, FolderOpen, Loader2, Search, Trash2 } from 'lucide-react';

import { ApiError, api } from '@/api/client';
import { useAccess } from '@/access/accessStore';
import { useLocale, useT } from '@/i18n';
import { useWorkflow } from '@/store/workflowStore';
import { ConfirmDialog, Dialog } from '@/ui/Dialogs';

type WorkflowRow = Awaited<ReturnType<typeof api.listWorkflows>>['workflows'][number];

export function WorkflowBrowser({ onClose }: { onClose: () => void }) {
  const t = useT();
  const locale = useLocale((state) => state.locale);
  const graphId = useWorkflow((state) => state.graphId);
  const dirty = useWorkflow((state) => state.dirty);
  const setGraph = useWorkflow((state) => state.setGraph);
  const canDelete = useAccess((state) => state.status.capabilities.workflow_delete);

  const [rows, setRows] = useState<WorkflowRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<WorkflowRow | null>(null);
  const [pendingOpen, setPendingOpen] = useState<WorkflowRow | null>(null);
  const [query, setQuery] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.listWorkflows();
      setRows(response.workflows);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : t('browser.listFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const openNow = async (id: string) => {
    setBusyId(id);
    setError(null);
    try {
      const graph = await api.getWorkflow(id);
      // `setGraph` rather than `replaceGraph`: opening a document is not an
      // edit, so it must not be undoable back into the previous workflow.
      setGraph(graph);
      onClose();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : t('browser.openFailed'));
    } finally {
      setBusyId(null);
    }
  };

  // Opening another workflow replaces the current document, so unsaved edits
  // would disappear without ever being mentioned. Ask first.
  const open = (row: WorkflowRow) => {
    if (dirty) {
      setPendingOpen(row);
      return;
    }
    void openNow(row.id);
  };

  const remove = async (row: WorkflowRow) => {
    setPendingDelete(null);
    setBusyId(row.id);
    try {
      await api.deleteWorkflow(row.id);
      setRows((previous) => previous.filter((item) => item.id !== row.id));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : t('browser.deleteFailed'));
    } finally {
      setBusyId(null);
    }
  };

  const visibleRows = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      [row.name ?? '', row.description ?? ''].some((value) =>
        value.toLowerCase().includes(needle),
      ),
    );
  }, [query, rows]);

  return (
    <>
      <Dialog
        title={t('browser.title')}
        wide
        icon={<FolderOpen size={14} className="dialog__icon" />}
        onClose={onClose}
        footer={
          <>
            <p className="field__hint grow">
              {dirty ? t('browser.dirty') : t('browser.saved')}
            </p>
            <button type="button" className="btn" onClick={onClose}>
              {t('browser.close')}
            </button>
          </>
        }
      >
        {error ? (
          <div className="callout callout--error">
            <AlertCircle size={13} className="callout__icon" />
            <span>{error}</span>
          </div>
        ) : null}

        <div className="browser__search">
          <Search size={13} className="browser__search-icon" />
          <input
            className="input"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('browser.search')}
            aria-label={t('browser.search')}
          />
        </div>

        {loading ? (
          <p className="field__hint">
            <Loader2 size={12} className="spin" /> {t('browser.loading')}
          </p>
        ) : visibleRows.length === 0 ? (
          <p className="empty">
            {rows.length === 0 ? t('browser.empty') : t('browser.noMatch', { query: query.trim() })}
            {rows.length === 0 ? (
              <>
                <br />
                <span className="dim">{t('browser.emptyHint')}</span>
              </>
            ) : null}
          </p>
        ) : (
          <ul className="browser">
            {visibleRows.map((row) => (
              <li key={row.id} className="browser__row">
                <button
                  type="button"
                  className="browser__open"
                  onClick={() => open(row)}
                  disabled={busyId !== null}
                >
                  <div className="browser__text">
                    <span className="browser__name truncate">
                      {row.name || t('browser.untitled')}
                      {row.id === graphId ? (
                        <span className="badge badge--accent">{t('browser.open')}</span>
                      ) : null}
                      {row.read_only ? (
                        <span className="badge badge--warning">{t('browser.readOnly')}</span>
                      ) : null}
                    </span>
                    {row.description ? (
                      <span className="browser__description truncate">{row.description}</span>
                    ) : null}
                    <span className="browser__meta mono">
                      {t(row.node_count === 1 ? 'browser.nodeCount.one' : 'browser.nodeCount.many', {
                        count: row.node_count,
                      })}{' '}
                      · {formatDate(row.updated_at, locale)}
                    </span>
                  </div>
                  {busyId === row.id ? <Loader2 size={13} className="spin" /> : <FolderOpen size={13} />}
                </button>

                {canDelete ? (
                  <button
                    type="button"
                    className="btn btn--ghost btn--icon btn--sm"
                    title={t('browser.delete')}
                    disabled={busyId !== null}
                    onClick={() => setPendingDelete(row)}
                  >
                    <Trash2 size={12} />
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Dialog>

      {pendingDelete ? (
        <ConfirmDialog
          title={t('browser.deleteTitle')}
          message={t('browser.deleteMessage', {
            name: pendingDelete.name || t('browser.untitled'),
          })}
          confirmLabel={t('browser.deleteConfirm')}
          danger
          onConfirm={() => void remove(pendingDelete)}
          onCancel={() => setPendingDelete(null)}
        />
      ) : null}

      {pendingOpen ? (
        <ConfirmDialog
          title={t('browser.discardTitle')}
          message={t('browser.discardMessage', {
            name: pendingOpen.name || t('browser.untitled'),
          })}
          confirmLabel={t('browser.discardConfirm')}
          danger
          onConfirm={() => {
            const row = pendingOpen;
            setPendingOpen(null);
            void openNow(row.id);
          }}
          onCancel={() => setPendingOpen(null)}
        />
      ) : null}
    </>
  );
}

/**
 * Format a timestamp in the interface language.
 *
 * The locale is threaded in rather than read from the store so this stays a
 * pure function — a date format is part of the language, but a helper that
 * silently depends on global state is harder to reason about.
 */
function formatDate(iso: string, locale: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(locale === 'zh' ? 'zh-CN' : 'en-US');
}
