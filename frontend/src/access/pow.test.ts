import { describe, expect, it } from 'vitest';

import { leadingZeroBitsHex, sha256Hex, solvePow } from '@/access/pow';

describe('proof of work', () => {
  it('matches a known SHA-256 vector', () => {
    expect(sha256Hex('abc')).toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    );
  });

  it('counts leading zero bits in a hex digest', () => {
    expect(leadingZeroBitsHex('ffff')).toBe(0);
    expect(leadingZeroBitsHex('7fff')).toBe(1);
    expect(leadingZeroBitsHex('00ff')).toBe(8);
    expect(leadingZeroBitsHex('0001')).toBe(15);
    expect(leadingZeroBitsHex('0000')).toBe(16);
  });

  it('finds a solution for a low difficulty', async () => {
    const solution = await solvePow('nonce', 8, { chunkSize: 10 });
    expect(solution).toBeGreaterThanOrEqual(0);
    expect(leadingZeroBitsHex(sha256Hex(`nonce:${solution}`))).toBeGreaterThanOrEqual(8);
  });

  it('stops when the signal is aborted', async () => {
    const controller = new AbortController();
    const promise = solvePow('nonce', 22, {
      signal: controller.signal,
      chunkSize: 1,
    });
    controller.abort();
    await expect(promise).rejects.toMatchObject({ name: 'AbortError' });
  });
});
