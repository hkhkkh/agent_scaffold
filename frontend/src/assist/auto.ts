/**
 * Auto mode: the assistant fills in a step while the user describes it.
 *
 * Someone who does not write code builds a graph by describing what each step
 * should do. Nothing used to happen after that description was written — the
 * node kept its empty prompt, ran green, and answered nothing, and the only way
 * out was a button the user had no reason to know was there. This closes that
 * gap, and it does exactly one thing: **a node whose description the user just
 * wrote has its settings derived from that description.**
 *
 * Two rules, and neither is negotiable:
 *
 *   1. **The assistant never overwrites the user.** It may write a setting that
 *      is unset, or one it wrote itself and nobody has touched since. A value a
 *      person typed is left exactly as it is, permanently.
 *   2. **Everything lands as an ordinary edit.** One history entry per action,
 *      so undo takes it back in one step and the document is marked unsaved —
 *      the same as if the user had typed it. There is no second kind of change
 *      that undo cannot reach.
 *
 * Failures are quiet by design. A model that is unreachable, slow, or has
 * nothing useful to say leaves the graph as the user left it; the Inspector
 * button is still there for anyone who wants to ask again, and the note left on
 * the node says so rather than leaving the user to guess.
 *
 * ---------------------------------------------------------------------------
 * What was tried here and removed: repairing the graph automatically.
 *
 * It worked, and it is gone, because of how it had to work. The only whole-graph
 * repair this repo has replaces the document wholesale, and the validator calls
 * a workflow with no Start node an *error* — so the very first thing a user does
 * in auto mode, dropping a node on an empty canvas, queued a repair that
 * rewrote everything. Observed, not theorised: it added a Start node of its own
 * accord, and the rewrite landed while a fill was in flight and threw the fill
 * away.
 *
 * An assistant that silently rewrites the whole canvas when someone drags a
 * node is the failure this product exists to avoid, so the automatic trigger is
 * out. Repairing stays a deliberate act with a diff to look at first. Making it
 * automatic needs an apply-a-diff mechanism, not a whole-document swap.
 * ---------------------------------------------------------------------------
 */

import { useEffect, useSyncExternalStore } from 'react';

import { api } from '../api/client';
import { useWorkflow } from '../store/workflowStore';
import { useAssistMode } from './mode';

/** Long enough that a sentence being typed is not one request per keystroke,
 *  short enough that the wait does not read as a wait. */
const FILL_DEBOUNCE_MS = 2000;

// ---------------------------------------------------------------------------
// What the assistant did, for the session only
// ---------------------------------------------------------------------------

/**
 * A note describing one thing the assistant did to one node.
 *
 * Held outside the store on purpose: this describes something that *happened*,
 * not something the document *says*. Putting it in the node's spec would mark
 * the workflow dirty every time the assistant helped, and would be saved into
 * the file and reloaded as if the user had written it.
 */
export interface AssistNote {
  /** Config keys the assistant filled in. */
  keys: string[];
  /** What it wrote for each of them, so a note can tell whether it still
   *  describes the node it is sitting on. */
  values: Record<string, unknown>;
  /** Set when the attempt did not land, so the UI can say so and offer the
   *  button instead of letting the node look untouched. */
  failed?: boolean;
}

let notes = new Map<string, AssistNote>();
const listeners = new Set<() => void>();

function publish(next: Map<string, AssistNote>): void {
  notes = next;
  for (const listener of listeners) listener();
}

function note(nodeId: string, value: AssistNote): void {
  const next = new Map(notes);
  next.set(nodeId, value);
  publish(next);
}

function subscribe(onChange: () => void): () => void {
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

/** What the assistant last did to this node, if anything this session. */
export function useAssistNote(nodeId: string | null): AssistNote | undefined {
  return useSyncExternalStore(subscribe, () => (nodeId ? notes.get(nodeId) : undefined));
}

/**
 * Every note at once, for a view that draws a whole graph.
 *
 * The map is replaced rather than mutated, so the reference is stable between
 * publishes and a subscriber re-renders when the assistant acts rather than on
 * every write to the store.
 */
export function useAssistNotes(): ReadonlyMap<string, AssistNote> {
  return useSyncExternalStore(subscribe, () => notes);
}

// ---------------------------------------------------------------------------
// Ownership
// ---------------------------------------------------------------------------

/**
 * Values the assistant wrote, so it can tell its own edits from the user's.
 *
 * A value that no longer matches what was recorded here was changed by a
 * person, and is never written again. Comparing values rather than tracking
 * "did the user touch this" needs no event plumbing and heals on its own: a
 * user who edits a field and then edits it back has, correctly, left it as the
 * assistant had it.
 */
const written = new Map<string, Map<string, unknown>>();

/** Which document `written` describes. Opening a different one invalidates it,
 *  because a node id from the old graph means nothing in the new one. */
let writtenFor: string | null = null;

function sameValue(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    // Circular or otherwise unserialisable: treat as different, which is the
    // safe direction — it means the assistant declines to write.
    return false;
  }
}

/**
 * Which of the settings the assistant wrote are still on the node.
 *
 * The note records a moment, but it sits on a card that shows the present. An
 * undo — or the user editing one of the fields — leaves the note claiming
 * credit for something that is no longer there, which is worse than saying
 * nothing at all. What it wrote and what is still there are compared here, and
 * only the surviving keys are its to claim.
 */
export function assistKeysInPlace(
  note: AssistNote,
  config: Record<string, unknown>,
): string[] {
  if (note.failed) return [];
  return note.keys.filter((key) => sameValue(config[key], note.values[key]));
}

/** Whether the note still has anything true to say about this node. */
export function assistNoteApplies(
  note: AssistNote | undefined,
  config: Record<string, unknown>,
): boolean {
  if (!note) return false;
  return note.failed === true || assistKeysInPlace(note, config).length > 0;
}

/**
 * The part of a suggestion the assistant is allowed to write.
 *
 * Keys are dropped rather than refused, and dropping one is not an error worth
 * reporting: a setting the user filled in is theirs, and leaving it alone is
 * the correct outcome, not a failed run.
 *
 * Exported because this is the rule the whole mode rests on — "the assistant
 * never overwrites the user" is a claim about this function and nothing else,
 * so it is tested directly rather than only through the loop that calls it.
 */
export function permitted(
  config: Record<string, unknown>,
  suggestion: Record<string, unknown>,
  mine: Map<string, unknown> | undefined,
): Record<string, unknown> {
  const allowed: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(suggestion)) {
    if (key in config) {
      const previous = mine?.get(key);
      if (previous === undefined || !sameValue(config[key], previous)) continue;
    }
    allowed[key] = value;
  }
  return allowed;
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------

/**
 * Run the assistant for as long as the mode is `auto`.
 *
 * Mounted once, by `App`. Everything it needs it reads from the store at the
 * moment it acts, so a stale closure cannot write a value computed from a graph
 * that has since changed.
 */
export function useAutoAssist(): void {
  const mode = useAssistMode();

  useEffect(() => {
    if (mode !== 'auto') return;

    /** node id -> the description already acted on. */
    const settled = new Map<string, string>();
    const timers = new Map<string, number>();
    let stopped = false;

    /**
     * Treat everything currently on the canvas as already handled.
     *
     * Auto mode reacts to what the user does from now on. Without this, opening
     * a finished workflow would send a request for every node in it — which is
     * not a hypothetical: seeding only at mount missed it, because the document
     * is loaded *after* the hook mounts, so all its descriptions looked like
     * something the user had just typed. The assistant quietly spent money
     * re-deciding settings that were already correct.
     *
     * Called at mount and whenever the open document is replaced.
     */
    let seededFor: string | null = null;
    const seed = (graphId: string) => {
      settled.clear();
      for (const timer of timers.values()) window.clearTimeout(timer);
      timers.clear();
      for (const node of useWorkflow.getState().nodes) {
        const description = node.data.spec.description.trim();
        if (description) settled.set(node.id, description);
      }
      // Only on a genuinely different document — a mode switch re-runs this and
      // must not make the assistant forget the values it wrote itself.
      if (writtenFor !== graphId) {
        written.clear();
        writtenFor = graphId;
      }
      seededFor = graphId;
    };
    seed(useWorkflow.getState().graphId);

    const fill = async (nodeId: string, description: string) => {
      if (stopped) return;
      try {
        const suggestion = await api.fillNode({
          graph: useWorkflow.getState().toGraph(),
          node_id: nodeId,
        });
        if (stopped) return;

        // Re-read both the node and its description. The text may have moved on
        // while the model was thinking, and settings derived from a description
        // the user has already replaced describe a node that no longer exists.
        const node = useWorkflow.getState().nodes.find((it) => it.id === nodeId);
        if (!node || node.data.spec.description.trim() !== description) return;

        const mine = written.get(nodeId);
        const allowed = permitted(
          node.data.spec.config ?? {},
          suggestion.config ?? {},
          mine,
        );
        if (Object.keys(allowed).length === 0) return;

        const changed = useWorkflow.getState().applyConfig(nodeId, allowed);
        if (changed.length === 0) return;

        const record = mine ?? new Map<string, unknown>();
        for (const key of changed) record.set(key, allowed[key]);
        written.set(nodeId, record);
        note(nodeId, {
          keys: changed,
          values: Object.fromEntries(changed.map((key) => [key, allowed[key]])),
        });
      } catch {
        // Auto mode cannot be allowed to interrupt. The node keeps whatever it
        // had, and the note is what stops that from looking like nothing
        // happened.
        if (!stopped) note(nodeId, { keys: [], values: {}, failed: true });
      }
    };

    const considerFill = () => {
      const state = useWorkflow.getState();
      // A replaced document is not a set of edits to it.
      if (state.graphId !== seededFor) seed(state.graphId);

      for (const node of state.nodes) {
        const description = node.data.spec.description.trim();
        if (!description || settled.get(node.id) === description) continue;
        // Recorded before the request, not after: an edit arriving while this
        // one is in flight is a new description and starts a new round, but a
        // failed request must not be retried on every subsequent store write.
        settled.set(node.id, description);

        const pending = timers.get(node.id);
        if (pending !== undefined) window.clearTimeout(pending);
        timers.set(
          node.id,
          window.setTimeout(() => {
            timers.delete(node.id);
            void fill(node.id, description);
          }, FILL_DEBOUNCE_MS),
        );
      }
    };

    const unsubscribe = useWorkflow.subscribe(considerFill);

    return () => {
      stopped = true;
      unsubscribe();
      for (const timer of timers.values()) window.clearTimeout(timer);
      timers.clear();
    };
  }, [mode]);
}
