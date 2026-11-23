/**
 * Proof-of-work fallback for the human-verification gate.
 *
 * The server signs the challenge, including its difficulty. This module only
 * searches for a solution; it never decides how hard the search is. `js-sha256`
 * is a small MIT dependency rather than a hand-rolled hash: the last thing a
 * gate should contain is custom cryptography.
 */

import { sha256 } from 'js-sha256';

export interface PowOptions {
  signal?: AbortSignal;
  onProgress?: (attempts: number) => void;
  /** Yield every N attempts so the UI can paint. Lower is smoother/slower. */
  chunkSize?: number;
}

export function sha256Hex(message: string): string {
  return sha256(message);
}

/** Count leading zero bits in a hex digest. */
export function leadingZeroBitsHex(hex: string): number {
  let bits = 0;
  for (const char of hex) {
    const nibble = Number.parseInt(char, 16);
    if (Number.isNaN(nibble)) return bits;
    if (nibble === 0) {
      bits += 4;
      continue;
    }
    if (nibble < 2) bits += 3;
    else if (nibble < 4) bits += 2;
    else if (nibble < 8) bits += 1;
    return bits;
  }
  return bits;
}

function abortError(): Error {
  if (typeof DOMException !== 'undefined') {
    return new DOMException('The challenge was cancelled.', 'AbortError');
  }
  return new Error('The challenge was cancelled.');
}

/**
 * Find the first non-negative integer whose `sha256("{nonce}:{n}")` has at
 * least `difficulty` leading zero bits.
 */
export async function solvePow(
  nonce: string,
  difficulty: number,
  options: PowOptions = {},
): Promise<number> {
  const chunkSize = Math.max(1, options.chunkSize ?? 2_000);
  let solution = 0;
  let sinceYield = 0;

  for (;;) {
    if (options.signal?.aborted) throw abortError();

    const hash = sha256(`${nonce}:${solution}`);
    if (leadingZeroBitsHex(hash) >= difficulty) {
      options.onProgress?.(solution + 1);
      return solution;
    }

    solution += 1;
    sinceYield += 1;
    if (sinceYield >= chunkSize) {
      options.onProgress?.(solution);
      sinceYield = 0;
      // Yield to the browser. `setTimeout(0)` is used instead of
      // requestAnimationFrame so a background tab still makes progress.
      await new Promise<void>((resolve) => setTimeout(resolve, 0));
    }
  }
}
