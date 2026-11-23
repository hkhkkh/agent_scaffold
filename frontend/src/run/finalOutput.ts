/**
 * The run's answer, derived the same way the backend derives it.
 *
 * Mirrors `backend/app/runtime/serialize.py::final_output`. It exists because the
 * **live** path reads the answer straight off the `run_finished` event's
 * `outputs` rather than waiting for a summary round-trip — and a second
 * implementation with quietly different rules is exactly the drift this project
 * keeps hitting (a value read two ways, disagreeing). `finalOutput.test.ts` pins
 * the cases that matter; keep the two in step.
 */

import type { FinalOutput } from '@/types/workflow';

/** Key an End node adds when `include_state` is on. A debug dump, not the answer. */
const STATE_DUMP_KEY = 'state';

/** Key the manager writes for a run stopped before reaching an End node. */
const PARTIAL_KEY = 'partial';

/**
 * Pick the run's answer out of `outputs`; `null` if there isn't one.
 *
 * `null` is the honest answer for three shapes: a partial dump, a payload with
 * no candidates once the state dump is set aside, and one with several. A wrong
 * pick is worse than a blank, because it looks like the agent said something it
 * never said.
 */
export function finalOutput(
  outputs: Record<string, unknown> | null | undefined,
): FinalOutput | null {
  if (!outputs || typeof outputs !== 'object') return null;
  // `=== true`, not truthiness: a node's own `output_label` could be "partial".
  if (outputs[PARTIAL_KEY] === true) return null;

  const candidates = Object.keys(outputs).filter((key) => key !== STATE_DUMP_KEY);
  if (candidates.length !== 1) return null;

  // `!` is safe: the length check above leaves exactly one candidate. Indexed
  // access is checked under `noUncheckedIndexedAccess`, which cannot see that.
  const key = candidates[0]!;
  return { key, value: outputs[key] };
}
