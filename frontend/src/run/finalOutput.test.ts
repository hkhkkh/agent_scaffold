/**
 * The client's copy of the backend's answer-picking rule.
 *
 * There are two implementations because there are two read paths: the live one
 * reads `run_finished`'s payload, the reloaded one reads a summary from the API.
 * Two implementations of one rule is a drift risk, so each case below names the
 * backend test it mirrors — when one side changes, the pair is what fails.
 *
 * See `backend/tests/test_final_output.py`.
 */

import { describe, expect, it } from 'vitest';

import { finalOutput } from '@/run/finalOutput';

describe('finalOutput', () => {
  it('takes a single key as the answer', () => {
    expect(finalOutput({ memo: 'hello' })).toEqual({ key: 'memo', value: 'hello' });
  });

  it('ignores the state dump `include_state` adds', () => {
    const outputs = { memo: 'hello', state: { vars: { a: 1 }, inputs: {} } };
    expect(finalOutput(outputs)).toEqual({ key: 'memo', value: 'hello' });
  });

  it('treats a null answer as an answer', () => {
    expect(finalOutput({ result: null })).toEqual({ key: 'result', value: null });
  });

  it('refuses to guess between two candidates', () => {
    expect(finalOutput({ a: 1, b: 2 })).toBeNull();
  });

  it('returns null for nothing at all', () => {
    expect(finalOutput({})).toBeNull();
    expect(finalOutput(null)).toBeNull();
    expect(finalOutput(undefined)).toBeNull();
  });

  it('rejects a real partial dump', () => {
    expect(finalOutput({ partial: true, vars: { n1: { out: 1 } } })).toBeNull();
  });

  it('rejects a lone partial marker', () => {
    expect(finalOutput({ partial: true })).toBeNull();
  });

  it('reads a false marker as an ordinary key', () => {
    // Why the check is `=== true`: a name is not a meaning.
    expect(finalOutput({ partial: false })).toEqual({ key: 'partial', value: false });
  });
});
