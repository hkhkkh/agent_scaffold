import { beforeEach, describe, expect, it } from 'vitest';

import { emptyGraph } from '@/types/workflow';
import { useWorkflow } from '@/store/workflowStore';
import { useCopilot, type CopilotConversation } from './copilotStore';

function conversation(id: string, graphId: string, text: string): CopilotConversation {
  return {
    id,
    title: text,
    at: '2026-10-23T00:00:00.000Z',
    graphId,
    messages: [{ id: `${id}-m`, role: 'user', text }],
    thinking: [],
  };
}

describe('copilot history follows the workflow', () => {
  beforeEach(() => {
    localStorage.clear();
    useCopilot.setState({
      conversationGraphId: '',
      conversations: [],
      messages: [],
      thinking: [],
      activeConversationId: null,
    });
  });

  it('restores only the conversation that belongs to the workflow', () => {
    useCopilot.setState({ conversations: [conversation('a1', 'wf-a', '第一条')] });
    localStorage.setItem('scaffold.copilot.current.v1:wf-a', JSON.stringify('a1'));

    useCopilot.getState().switchWorkflow('wf-a');
    expect(useCopilot.getState().messages.map((entry) => entry.text)).toEqual(['第一条']);

    useCopilot.getState().switchWorkflow('wf-b');
    expect(useCopilot.getState().messages).toEqual([]);
    expect(useCopilot.getState().activeConversationId).toBeNull();
  });

  it('moves old turn-level reasoning onto its assistant message', () => {
    const legacy: CopilotConversation = {
      id: 'legacy',
      title: '旧对话',
      at: '2026-10-23T00:00:00.000Z',
      graphId: 'wf-a',
      messages: [
        { id: 'u', role: 'user', text: '为什么？' },
        { id: 'a', role: 'assistant', text: '因为……' },
      ],
      thinking: ['先看后端的顺序。'],
    };
    useCopilot.setState({ conversations: [legacy] });
    localStorage.setItem('scaffold.copilot.current.v1:wf-a', JSON.stringify('legacy'));

    useCopilot.getState().switchWorkflow('wf-a');

    const messages = useCopilot.getState().messages;
    expect(messages[1]?.thinking).toEqual(['先看后端的顺序。']);
    expect(messages[0]?.thinking).toBeUndefined();
  });

  it('keeps the transcript when a streamed snapshot keeps the document id', () => {
    // The backend used to mint a new graph id for every live snapshot. The
    // canvas read that as a document switch and cleared the conversation after
    // the agent appeared. A snapshot with the same id must be a no-op here.
    useWorkflow.setState({ graphId: 'wf-a' });
    useCopilot.setState({
      conversationGraphId: 'wf-a',
      messages: [{ id: 'u', role: 'user', text: '做一个 agent' }],
      activeConversationId: 'c1',
    });

    useWorkflow.getState().applyLiveGraph({
      ...emptyGraph(),
      id: 'wf-a',
      name: 'Agent',
    });

    expect(useCopilot.getState().messages.map((entry) => entry.text)).toEqual([
      '做一个 agent',
    ]);
    expect(useCopilot.getState().activeConversationId).toBe('c1');
  });

  it('ignores a pointer that names another workflow conversation', () => {
    useCopilot.setState({
      conversations: [conversation('b1', 'wf-b', '别人的对话')],
    });
    localStorage.setItem('scaffold.copilot.current.v1:wf-a', JSON.stringify('b1'));

    useCopilot.getState().switchWorkflow('wf-a');

    expect(useCopilot.getState().messages).toEqual([]);
  });
});
