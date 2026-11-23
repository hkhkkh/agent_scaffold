import { render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { RunPanel } from './RunPanel';
import { useWorkflow } from '@/store/workflowStore';
import type { TrialRunResult } from '@/types/workflow';

function trial(): TrialRunResult {
  return {
    status: 'failed',
    ok: false,
    summary: 'Trial run failed at "Search".',
    error: 'web_search is not configured',
    failed_node: 'tool_1',
    failed_node_label: 'Search',
    duration_ms: 1234,
    outputs: {},
    trace: [
      {
        kind: 'node_started',
        node_id: 'tool_1',
        node: 'Search',
        inputs: { query: 'x' },
      },
      {
        kind: 'node_failed',
        node_id: 'tool_1',
        node: 'Search',
        error: 'web_search is not configured',
        duration_ms: 1200,
      },
      {
        kind: 'log',
        node_id: 'tool_1',
        node: 'Search',
        message: 'tool call rejected',
        level: 'error',
      },
    ],
  };
}

describe('Run panel trial run', () => {
  beforeEach(() => {
    localStorage.clear();
    useWorkflow.setState({
      runId: null,
      runStatus: null,
      runEvents: [],
      stepByNode: {},
      selfTest: null,
    });
  });

  it('shows the copilot trial with its per-node steps', () => {
    useWorkflow.setState({ selfTest: trial() });
    const { container } = render(<RunPanel onRun={() => undefined} />);

    expect(container.querySelector('.run__selftest')).not.toBeNull();
    expect(container.textContent).toContain('Search');
    expect(container.textContent).toContain('web_search is not configured');
    // The failed step is rendered, not just the summary.
    expect(container.querySelectorAll('.run-step').length).toBe(1);
    expect(container.querySelector('.run-step--failed')).not.toBeNull();
  });
});
