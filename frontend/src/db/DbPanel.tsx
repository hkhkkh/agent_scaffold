/**
 * Live view of the results database.
 *
 * This is the panel that answers "is the agent actually doing anything?" — the
 * run timeline shows nodes lighting up, but the row count is the part that only
 * ever goes up, and watching it climb while the graph is still working is the
 * proof that the writes are real rather than a claim in a log line.
 *
 * **Polling, not SSE.** The writer is whichever run happens to be writing —
 * not necessarily the one in the foreground — and the panel has to be correct
 * after a run finishes and after a page reload. A snapshot is idempotent in a
 * way a stream is not, and one request a second against a local SQLite file is
 * not a cost worth engineering around. The interval is *adaptive* rather than
 * fixed, though: while a run is live, a second is right; while nothing is
 * running, a request a second to redraw the same numbers is pure noise, so it
 * drops to a slow heartbeat.
 */

import { useEffect, useRef, useState } from 'react';
import { Database, Loader2 } from 'lucide-react';

import { api, type LabDatabase, type LabTable } from '@/api/client';
import { useT } from '@/i18n';
import { useWorkflow } from '@/store/workflowStore';

/** How often to ask, depending on whether anything can be writing. */
const POLL_LIVE_MS = 1000;
const POLL_IDLE_MS = 5000;

/** How many latest rows to show per table. */
const PREVIEW_ROWS = 6;

/** How long the "+N" badge and the colour flash stay up. */
const BUMP_MS = 1500;

/** Fields rendered as a link rather than as text. */
const URL_FIELDS = /^(source_url|url|link)$/;

export function DbPanel() {
  const t = useT();
  const runStatus = useWorkflow((state) => state.runStatus);
  const running = runStatus === 'running' || runStatus === 'pending';

  const [data, setData] = useState<LabDatabase | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [delta, setDelta] = useState(0);
  const [bumped, setBumped] = useState(false);
  const [newIds, setNewIds] = useState<ReadonlySet<number>>(() => new Set());

  // The previous total lives in a ref, not in state: it is a value to compare
  // against, and putting it in state would make every poll schedule a second
  // render that changes nothing on screen.
  const lastTotal = useRef<number | null>(null);
  // The highest `_row_id` seen so far. Deliberately an id and not a row count:
  // `_row_id` is AUTOINCREMENT, so after the table is dropped and recreated —
  // which is how a workflow that starts from a clean slate behaves — ids carry
  // on from where they left off. Comparing ids against a count would then mark
  // every row as brand new on the first poll after a reset.
  const lastMaxId = useRef<number>(0);
  const bumpTimer = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | null = null;

    const tick = async () => {
      try {
        const snapshot = await api.labDb(PREVIEW_ROWS);
        if (cancelled) return;

        setData(snapshot);
        setError(null);

        // Every row written since the previous poll, whichever table it landed
        // in. Collected before the refs move, so the first poll of a session
        // marks nothing — a page that flashes its entire contents green on load
        // is telling the reader nothing.
        const seenBefore = lastMaxId.current;
        const fresh = new Set<number>();
        let maxId = seenBefore;
        for (const table of snapshot.tables) {
          for (const row of table.preview) {
            const id = row._row_id;
            if (typeof id !== 'number') continue;
            if (id > seenBefore && seenBefore > 0) fresh.add(id);
            if (id > maxId) maxId = id;
          }
        }
        lastMaxId.current = maxId;
        setNewIds(fresh);

        const previous = lastTotal.current;
        const total = snapshot.total_rows;
        if (previous !== null && total > previous) {
          setDelta(total - previous);
          setBumped(true);
          if (bumpTimer.current) window.clearTimeout(bumpTimer.current);
          bumpTimer.current = window.setTimeout(() => setBumped(false), BUMP_MS);
        }
        lastTotal.current = total;
      } catch (err) {
        if (cancelled) return;
        // A failed poll is not worth an error banner — the backend may be
        // restarting — but staying silent about it would leave a stale count on
        // screen looking live.
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!cancelled) {
          timer = window.setTimeout(tick, running ? POLL_LIVE_MS : POLL_IDLE_MS);
        }
      }
    };

    void tick();

    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
      if (bumpTimer.current) window.clearTimeout(bumpTimer.current);
    };
  }, [running]);

  const total = data?.total_rows ?? 0;
  const tables = data?.tables ?? [];

  return (
    <div className="db">
      <div className="db__head">
        <span className="db__title">
          <Database size={12} />
          {t('db.title')}
        </span>
        <span className={`db__live${running ? ' db__live--on' : ''}`} />
        <span className="db__spacer" />
        {running ? (
          <span className="dim" style={{ fontSize: 'var(--text-xs)' }}>
            {t('db.watching')}
          </span>
        ) : null}
      </div>

      <div className="db__count">
        <span className={`db__count-value${bumped ? ' db__count-value--bump' : ''}`}>
          {total}
        </span>
        <span className="db__count-label">{t('db.rowsLabel')}</span>
        {bumped && delta > 0 ? <span className="db__delta">+{delta}</span> : null}
      </div>

      <div className="db__body scroll">
        {error && !data ? (
          <p className="db__empty">{t('db.unreachable')}</p>
        ) : tables.length === 0 ? (
          <p className="db__empty">
            {t('db.empty')}
            <br />
            <span className="dim">{t('db.emptyHint')}</span>
          </p>
        ) : (
          tables.map((table) => (
            <TableCard key={table.name} table={table} newIds={newIds} />
          ))
        )}
      </div>

      <div className="db__foot" title={data?.path ?? ''}>
        {data?.path ? (
          <>
            <Database size={10} />
            {data.path}
          </>
        ) : (
          <>
            <Loader2 size={10} className="spin" />
            {t('db.loading')}
          </>
        )}
      </div>
    </div>
  );
}

/**
 * One table, with its newest rows.
 *
 * Rows written by the change that triggered the last flash are outlined, so
 * "the count went up" and "here is what went in" are the same piece of
 * information rather than two the eye has to connect.
 */
function TableCard({ table, newIds }: { table: LabTable; newIds: ReadonlySet<number> }) {
  const fields = table.columns.filter((column) => !column.startsWith('_'));

  return (
    <div className="db__table">
      <div className="db__table-head">
        <span className="db__table-name">{table.name}</span>
        <span className="db__table-rows">{table.rows}</span>
      </div>

      {table.preview.map((row) => {
        const id = typeof row._row_id === 'number' ? row._row_id : null;
        const isNew = id !== null && newIds.has(id);

        return (
          <div key={String(id ?? JSON.stringify(row).slice(0, 40))} className={`db__row${isNew ? ' db__row--new' : ''}`}>
            <div className="db__row-id">
              #{String(row._row_id ?? '?')}
              {typeof row._inserted_at === 'string' ? (
                <span>{row._inserted_at.slice(11, 19)}</span>
              ) : null}
            </div>
            {fields.map((field) => {
              const value = row[field];
              if (value === null || value === undefined || value === '') return null;
              const text = typeof value === 'string' ? value : JSON.stringify(value);
              return (
                <div className="db__field" key={field}>
                  <span className="db__field-key">{field}</span>
                  <span
                    className={`db__field-value${
                      URL_FIELDS.test(field) ? ' db__field-value--url' : ''
                    }`}
                  >
                    {text.length > 260 ? `${text.slice(0, 260)}…` : text}
                  </span>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}
