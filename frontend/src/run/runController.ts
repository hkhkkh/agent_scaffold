/**
 * The run lifecycle, outside React.
 *
 * It lives here rather than in `App` because two views now drive runs — the
 * canvas editor and the use view — and a `useRef` per view would mean two live
 * SSE streams racing to write the same store fields. One module-level stream,
 * one owner.
 *
 * A module rather than a store slice, deliberately: the store is a pure state
 * reducer (`applyRunEvent`), and the transport does not belong in it.
 *
 * This also fixed a real breakage — `App`'s copy of `syncRun` called `api`
 * without importing it, so the fallback threw `ReferenceError` exactly when it
 * was needed. There is one copy now, and it type-checks.
 */

import { api, streamRun, type RunStream } from '@/api/client';
import { useWorkflow } from '@/store/workflowStore';
import type { RunStatus, RunStep } from '@/types/workflow';

let stream: RunStream | null = null;

/** How often :func:`watchRun` re-reads a run that is still going. */
const WATCH_INTERVAL_MS = 1500;

/**
 * Statuses a run will still move out of.
 *
 * Written as a deny-list rather than `=== 'succeeded'` so a terminal status the
 * TS union has not caught up with yet — `stopped`, which the backend emits and
 * `RunStatus` still omits — settles instead of being polled forever.
 */
const IN_FLIGHT: ReadonlySet<RunStatus> = new Set<RunStatus>([
  'pending',
  'queued',
  'running',
]);

/** Drop the current stream. Safe to call when there is none. */
export function closeRunStream(): void {
  stream?.close();
  stream = null;
}

/**
 * Re-read a run from the API and fold it into the store.
 *
 * The safety net under the stream: SSE can end early (a proxy, a sleeping
 * laptop) while the run is still finishing server-side. `finishRun` takes the
 * authoritative summary, including the spend figure the events only estimate.
 */
export async function syncRun(id: string): Promise<void> {
  try {
    const summary = await api.getRun(id);
    const { steps } = await api.getRunSteps(id);
    if (summary) useWorkflow.getState().finishRun(summary);
    if (steps) {
      const stepByNode: Record<string, RunStep> = {};
      for (const step of steps) stepByNode[step.node_id] = step;
      useWorkflow.setState({ stepByNode });
    }
  } catch {
    // A failed resync is not worth surfacing: the run's own error, if any, is
    // already in the store, and a flaky read must not overwrite a real message.
  }
}

/**
 * Re-read a running run until it settles; returns a stop function.
 *
 * The stream is the fast path. This catches what it misses: a frame dropped by a
 * proxy, or a stream that closes without its terminal event. Only the *settled*
 * transition is applied, so it cannot fight the stream over a run still in
 * flight, and the caller's own cleanup stops it once the status changes.
 */
export function watchRun(id: string): () => void {
  const check = async () => {
    try {
      const summary = await api.getRun(id);
      if (IN_FLIGHT.has(summary.status)) return;
      const { steps } = await api.getRunSteps(id);
      useWorkflow.getState().finishRun(summary);
      if (steps) {
        const stepByNode: Record<string, RunStep> = {};
        for (const step of steps) stepByNode[step.node_id] = step;
        useWorkflow.setState({ stepByNode });
      }
    } catch {
      // A failed poll is expected while the run is still being set up; the next
      // tick retries, so there is nothing to report.
    }
  };
  const interval = window.setInterval(() => void check(), WATCH_INTERVAL_MS);
  return () => window.clearInterval(interval);
}

/** Subscribe to a run's event stream, replacing any stream already open. */
export function startStream(id: string): void {
  closeRunStream();
  stream = streamRun(id, {
    onEvent: (event) => useWorkflow.getState().applyRunEvent(event),
    onError: () => void syncRun(id),
    onClose: () => void syncRun(id),
  });
}

/**
 * Start a run and stream it. Returns the run id, or `null` when the start itself
 * failed — in which case `startRun` has recorded the reason and the panel shows
 * it, so there is nothing to stream and no second error to raise.
 */
export async function beginRun(inputs: Record<string, unknown>): Promise<string | null> {
  closeRunStream();
  await useWorkflow.getState().startRun(inputs);
  const id = useWorkflow.getState().runId;
  if (id) startStream(id);
  return id;
}

/**
 * Resume streaming once a human input node has been answered.
 *
 * `HumanInputDialog` dispatches a DOM event rather than importing this module,
 * so a presentational component stays free of transport concerns — the same
 * trade `BaseNode` makes for canvas edits.
 */
export function installRunBridge(): () => void {
  const onResumed = (event: Event) => {
    const detail = (event as CustomEvent<{ runId: string }>).detail;
    if (detail?.runId) startStream(detail.runId);
  };
  window.addEventListener('scaffold:run-resumed', onResumed);
  return () => window.removeEventListener('scaffold:run-resumed', onResumed);
}
