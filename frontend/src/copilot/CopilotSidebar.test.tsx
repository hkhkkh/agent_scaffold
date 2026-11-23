import { fireEvent, render } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CopilotSidebar } from './CopilotSidebar';
import { useCopilot } from './copilotStore';

describe('copilot composer', () => {
  beforeEach(() => {
    localStorage.clear();
    useCopilot.setState({
      messages: [],
      thinking: [],
      running: false,
      waiting: false,
      stages: [],
      trace: [],
      streamingId: null,
      historyOpen: false,
      conversations: [],
      activeConversationId: null,
    });
  });

  it('Enter sends and Shift+Enter inserts a newline', () => {
    const send = vi.fn();
    useCopilot.setState({ send });

    const { container } = render(<CopilotSidebar />);
    const input = container.querySelector('.copilot__input') as HTMLTextAreaElement;

    fireEvent.change(input, { target: { value: '把日报时间改到 08:00' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: true });
    expect(send).not.toHaveBeenCalled();

    fireEvent.keyDown(input, { key: 'Enter' });
    expect(send).toHaveBeenCalledWith(
      '把日报时间改到 08:00',
      expect.objectContaining({ expectedOutput: '' }),
    );
  });

  it('shows every edit, validation and trial run in order', () => {
    useCopilot.setState({
      trace: [
        { id: 't1', kind: 'tool', name: 'add_node', detail: '{"type":"llm"}', status: 'ok' },
        { id: 't2', kind: 'validation', name: 'validation_failed', detail: 'missing_edge: start has no outgoing edge', status: 'failed' },
        { id: 't3', kind: 'correction', name: 'runtime', detail: 'start · failed', status: 'running' },
        { id: 't4', kind: 'test', name: 'self_test', status: 'ok' },
      ],
    });
    const { container } = render(<CopilotSidebar />);
    const items = container.querySelectorAll('.copilot-trace__item');
    expect(items).toHaveLength(4);
    expect(container.textContent).toContain('add_node');
    expect(container.textContent).toContain('missing_edge');
    expect(container.textContent).toContain('self_test');
    expect(container.querySelector('.copilot-trace')).not.toBeNull();
    expect(container.querySelector('.copilot-trace__item--failed')).not.toBeNull();
  });
});
