import { describe, expect, it } from 'vitest';

import { inputsForText } from './inputs';

function node(type: string, schema?: unknown) {
  return { data: { spec: { type, config: { input_schema: schema } } } };
}

describe('inputsForText', () => {
  it('writes the sentence into the declared message field and keeps other defaults', () => {
    const nodes = [node('start', { message: '工作区里有哪些文件？', history: '' })];
    expect(inputsForText(nodes, '你好')).toEqual({ message: '你好', history: '' });
  });

  it('prefers message over other declared string fields', () => {
    const nodes = [node('start', { note: 'x', message: 'hi', query: 'q' })];
    expect(inputsForText(nodes, 'text')).toEqual({ note: 'x', message: 'text', query: 'q' });
  });

  it('falls back to the first declared string field', () => {
    const nodes = [node('start', { rows: 3, keyword: 'k' })];
    expect(inputsForText(nodes, 'foo')).toEqual({ rows: 3, keyword: 'foo' });
  });

  it('falls back to query when there is no Start node', () => {
    expect(inputsForText([], 'hello')).toEqual({ query: 'hello' });
  });
});
