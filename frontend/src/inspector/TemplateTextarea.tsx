/**
 * A textarea that completes `{{ }}` paths from the graph.
 *
 * Typing `{{` (or a partial `{{ vars.pl`) opens a list of paths that actually
 * exist in this workflow. The list comes from `templateCompletions.ts`, so the
 * Loop's top-level `iteration` and the fields a node produced in the last run
 * are offered without the author having to remember them.
 *
 * The menu is anchored to the field rather than to the caret. That is a
 * deliberate MVP trade-off: caret tracking needs a mirrored off-screen element
 * to measure text, and getting it wrong is worse than a menu that appears in a
 * predictable place.
 */

import { useMemo, useRef, useState, type ChangeEvent, type KeyboardEvent } from 'react';

import { useT } from '@/i18n';
import {
  filterTemplateCandidates,
  type TemplateCandidate,
} from '@/inspector/templateCompletions';

export function TemplateTextarea({
  id,
  value,
  rows,
  placeholder,
  invalidClass,
  candidates,
  code = false,
  spellCheck,
  onChange,
}: {
  id: string;
  value: string;
  rows: number;
  placeholder: string;
  invalidClass: string;
  candidates: TemplateCandidate[];
  /** Renders the monospace JSON/code textarea style. */
  code?: boolean;
  spellCheck?: boolean;
  onChange: (value: string) => void;
}) {
  const t = useT();
  const textarea = useRef<HTMLTextAreaElement>(null);
  const trigger = useRef({ start: 0, caret: 0 });
  const [open, setOpen] = useState(false);
  const [prefix, setPrefix] = useState('');
  const [active, setActive] = useState(0);

  const filtered = useMemo(
    () => filterTemplateCandidates(candidates, prefix),
    [candidates, prefix],
  );

  const reconsider = (event: ChangeEvent<HTMLTextAreaElement>) => {
    const next = event.target.value;
    onChange(next);

    // `isComposing` is true while an IME is still assembling a character. A
    // menu opening in the middle of Chinese input is noise, and the trigger
    // (`{{`) is ASCII anyway.
    if ((event.nativeEvent as InputEvent).isComposing) return;

    const caret = event.target.selectionStart ?? next.length;
    const match = next.slice(0, caret).match(/\{\{\s*([A-Za-z0-9_.]*)$/);
    if (!match || match.index === undefined) {
      setOpen(false);
      return;
    }
    trigger.current = { start: match.index, caret };
    setPrefix(match[1] ?? '');
    setActive(0);
    setOpen(true);
  };

  const insert = (candidate: TemplateCandidate) => {
    const { start, caret } = trigger.current;
    const before = value.slice(0, start);
    const after = value.slice(caret);
    // Consume closing braces the user already typed, with or without a space.
    const closing = after.match(/^\s*\}\}/);
    const rest = closing ? after.slice(closing[0].length) : after;
    const inserted = `{{ ${candidate.path} }}`;
    const next = before + inserted + rest;

    onChange(next);
    setOpen(false);
    requestAnimationFrame(() => {
      const element = textarea.current;
      if (!element) return;
      element.focus();
      const position = before.length + inserted.length;
      element.setSelectionRange(position, position);
    });
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!open) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (filtered.length === 0) return;
      const delta = event.key === 'ArrowDown' ? 1 : -1;
      setActive((current) => (current + delta + filtered.length) % filtered.length);
      return;
    }
    if (event.key === 'Enter' || event.key === 'Tab') {
      const chosen = filtered[active];
      if (chosen) {
        event.preventDefault();
        insert(chosen);
      }
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      setOpen(false);
    }
  };

  return (
    <div className="template-field">
      <textarea
        ref={textarea}
        id={id}
        className={`textarea${code ? ' textarea--code' : ''}${invalidClass}`}
        value={value}
        rows={rows}
        placeholder={placeholder}
        spellCheck={spellCheck}
        onChange={reconsider}
        onKeyDown={onKeyDown}
        onBlur={() => window.setTimeout(() => setOpen(false), 120)}
        role="combobox"
        aria-expanded={open}
        aria-autocomplete="list"
        aria-controls={`${id}-template-menu`}
      />

      {open ? (
        <div
          id={`${id}-template-menu`}
          className="template-menu"
          role="listbox"
          aria-label={t('template.menu')}
        >
          <div className="template-menu__hint">{t('template.hint')}</div>
          {filtered.length === 0 ? (
            <div className="template-menu__empty">{t('template.noMatch')}</div>
          ) : (
            filtered.map((candidate, index) => (
              <button
                key={candidate.path}
                type="button"
                role="option"
                aria-selected={index === active}
                className={`template-menu__item${
                  index === active ? ' template-menu__item--active' : ''
                }`}
                // `mousedown` rather than `click`: it fires before the textarea
                // blurs, so the selection lands in the field instead of closing it.
                onMouseDown={(event) => {
                  event.preventDefault();
                  insert(candidate);
                }}
                onMouseEnter={() => setActive(index)}
              >
                <span className="template-menu__path mono">{candidate.path}</span>
                <span className="template-menu__detail">
                  {candidate.group} · {candidate.detail}
                </span>
              </button>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}
