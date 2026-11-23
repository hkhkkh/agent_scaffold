import { describe, expect, it } from 'vitest';

import { buildLines } from './UseView';

function log(seq: number, message: string, level = 'info') {
  return {
    id: `e-${seq}`,
    type: 'log',
    seq,
    ts: '',
    data: { message, level },
  } as never;
}

describe('use view conversation lines', () => {
  it('keeps a human-readable progress line', () => {
    const lines = buildLines([log(1, '第 1/3 步 正在: 数据助理')]);
    expect(lines.map((line) => line.text)).toEqual(['第 1/3 步 正在: 数据助理']);
  });

  it('drops engineering telemetry and template warnings', () => {
    const lines = buildLines([
      log(1, 'Start resolved 3 input field(s); using defaults for: history'),
      log(2, '[warning] history, message did not resolve; sent the context value instead.', 'warning'),
      log(3, 'Agent started with 10 tool(s) and a 8-step budget.'),
      log(4, 'Agent used 4072 token(s) over 2 call(s) (4024 in / 48 out).'),
      log(5, "End accepted result for 'answer': '...'"),
      log(6, 'Step 1: calling file_list({})'),
      log(7, '第 1/3 步 正在: 数据助理'),
    ]);
    expect(lines.map((line) => line.text)).toEqual(['第 1/3 步 正在: 数据助理']);
  });
});
