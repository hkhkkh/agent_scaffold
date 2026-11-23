/**
 * Map the use view's single sentence onto the workflow's own Start contract.
 *
 * The use view is deliberately one box for every agent, but the graph still
 * declares what the entry point expects. Sending a fixed `query` to a workflow
 * that declares `message` makes its prompt template miss the user's question
 * and fall back to the raw context object. The declared keys win; only when the
 * graph declares no text input at all does the historical `query` convention
 * apply (so an invalid graph still fails validation for the real reason).
 */

/** Fields the platform prefers when one sentence has to fill one slot. */
const PREFERRED_TEXT_FIELDS = ['message', 'query', 'text', 'input', 'prompt', 'question'];

export interface InputNode {
  data: {
    spec: {
      type: string;
      config: { input_schema?: unknown };
    };
  };
}

export function inputsForText(
  nodes: ReadonlyArray<InputNode>,
  text: string,
): Record<string, unknown> {
  const start = nodes.find((node) => node.data.spec.type === 'start');
  const declared = start?.data.spec.config.input_schema;

  if (declared && typeof declared === 'object' && !Array.isArray(declared)) {
    const fields = declared as Record<string, unknown>;
    const stringFields = Object.keys(fields).filter((key) => typeof fields[key] === 'string');
    const key =
      PREFERRED_TEXT_FIELDS.find((candidate) => stringFields.includes(candidate)) ??
      stringFields[0];
    if (key) return { ...fields, [key]: text };
  }

  return { query: text };
}
