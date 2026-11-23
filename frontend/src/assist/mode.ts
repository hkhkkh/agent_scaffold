/**
 * How much the assistant is allowed to do on its own.
 *
 * Two people use this product and they want opposite things from the same
 * machinery. Someone who does not write code wants the graph to keep working
 * without being told how; someone who does wants the machine to stay out of the
 * way until asked. One behaviour cannot serve both, so the switch is explicit
 * rather than guessed.
 *
 * A preference, not part of the document — kept out of `workflowStore` for the
 * same reason the language is: changing it must not mark the workflow dirty or
 * land in the undo stack.
 *
 *   auto    The assistant fixes what is provably broken, and fills a node in
 *           from its description, as the user works. Every change is an ordinary
 *           edit, so undo works on it like any other.
 *   manual  The assistant does nothing until a button is pressed.
 */

import { useSyncExternalStore } from 'react';

export type AssistMode = 'auto' | 'manual';

const STORAGE_KEY = 'scaffold.assist';

/** Where a first-time visitor lands: the default has to serve the person who
 *  cannot recover from a broken graph, because the other one can switch. */
export const DEFAULT_ASSIST_MODE: AssistMode = 'auto';

function isMode(value: unknown): value is AssistMode {
  return value === 'auto' || value === 'manual';
}

function read(): AssistMode {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (isMode(saved)) return saved;
  } catch {
    // Storage can be unavailable (private mode, blocked cookies). Falling back
    // to the default is correct; a preference that cannot be read is not an
    // error worth surfacing.
  }
  return DEFAULT_ASSIST_MODE;
}

// A module-level value plus subscribers rather than a store: this is one enum
// with one setter, and pulling in a state library for it would be more
// machinery than the thing it holds.
let current: AssistMode = read();
const listeners = new Set<(mode: AssistMode) => void>();

export function getAssistMode(): AssistMode {
  return current;
}

export function setAssistMode(mode: AssistMode): void {
  if (mode === current) return;
  current = mode;
  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // Not persisting the choice is survivable; refusing to apply it is not.
  }
  for (const listener of listeners) listener(mode);
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

/** Subscribe to the mode. Components re-render when it changes. */
export function useAssistMode(): AssistMode {
  return useSyncExternalStore(
    subscribe,
    () => current,
    () => DEFAULT_ASSIST_MODE,
  );
}
