/**
 * Locale state and lookup.
 *
 * Kept in its own store rather than inside `workflowStore` because it is a
 * *preference*, not part of the document: switching language must not mark the
 * workflow dirty or land in the undo stack.
 *
 * Two entry points, because not every caller is a component:
 *
 *  - `useT()` — a hook; subscribes, so the UI re-renders on switch.
 *  - `translate()` — reads the current locale once; for event handlers and the
 *    store, which cannot call hooks.
 */

import { useMemo } from 'react';
import { create } from 'zustand';

import { LOCALE_LABELS, TRANSLATIONS, type Locale, type MessageKey } from './translations';

export { LOCALE_LABELS };
export type { Locale, MessageKey };

const STORAGE_KEY = 'scaffold.locale';

/** Languages the backend catalogue can be fetched in. */
export const SUPPORTED_LOCALES: Locale[] = ['en', 'zh'];

function isLocale(value: unknown): value is Locale {
  return value === 'en' || value === 'zh';
}

/**
 * Pick a starting language.
 *
 * A saved choice wins; otherwise the browser's preference decides, so a Chinese
 * user gets a Chinese interface on first visit without hunting for a toggle.
 */
function detectLocale(): Locale {
  try {
    const saved = window.localStorage.getItem(STORAGE_KEY);
    if (isLocale(saved)) return saved;
  } catch {
    // Storage can be unavailable (private mode, blocked cookies). Falling
    // through to detection is correct; failing to read a preference is not an
    // error worth surfacing.
  }

  const candidates =
    typeof navigator !== 'undefined' && navigator.languages?.length
      ? navigator.languages
      : typeof navigator !== 'undefined' && navigator.language
        ? [navigator.language]
        : [];

  for (const tag of candidates) {
    if (tag.toLowerCase().startsWith('zh')) return 'zh';
  }
  return 'en';
}

interface LocaleState {
  locale: Locale;
  setLocale: (locale: Locale) => void;
}

export const useLocale = create<LocaleState>((set) => ({
  locale: detectLocale(),
  setLocale: (locale) => {
    set({ locale });
    try {
      window.localStorage.setItem(STORAGE_KEY, locale);
    } catch {
      // The switch still applies for this session; only persistence is lost.
    }
  },
}));

/** Substitute `{name}` placeholders. Unknown names are left visible on purpose. */
function interpolate(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, key: string) => {
    const value = vars[key];
    return value === undefined ? match : String(value);
  });
}

/** Translate using the current locale, without subscribing. */
export function translate(
  key: MessageKey,
  vars?: Record<string, string | number>,
): string {
  const table = TRANSLATIONS[useLocale.getState().locale];
  return interpolate(table[key] ?? key, vars);
}

export type TFunction = (key: MessageKey, vars?: Record<string, string | number>) => string;

/**
 * Translate, and re-render when the language changes.
 *
 * The returned function is memoised per locale so it can be listed in a
 * dependency array without defeating `useMemo`/`useCallback` downstream.
 */
export function useT(): TFunction {
  const locale = useLocale((state) => state.locale);

  return useMemo<TFunction>(() => {
    const table = TRANSLATIONS[locale];
    return (key, vars) => interpolate(table[key] ?? key, vars);
  }, [locale]);
}

/** Plural helper: English needs the distinction, Chinese does not. */
export function usePlural(): (
  count: number,
  one: MessageKey,
  many: MessageKey,
) => string {
  const t = useT();
  return useMemo(
    () => (count: number, one: MessageKey, many: MessageKey) =>
      t(count === 1 ? one : many, { count }),
    [t],
  );
}
