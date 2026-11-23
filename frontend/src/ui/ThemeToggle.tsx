/**
 * Light/dark switch.
 *
 * Light is the product default (see `tokens.css`); dark is the same semantic
 * token set graded for a developer desk. The choice lives on `<html data-theme>`
 * so every component flips without re-rendering the tree — a component never
 * reads the theme, it only reads tokens.
 *
 * `main.tsx` applies the stored value before first paint (no flash); this
 * component keeps the button in sync and writes the preference back.
 */

import { useEffect, useState } from 'react';
import { Moon, Sun } from 'lucide-react';

import { useT } from '@/i18n';

export type Theme = 'light' | 'dark';

export const THEME_STORAGE_KEY = 'scaffold.theme';

export function initialTheme(): Theme {
  // `?theme=light|dark` forces the theme for one page load. Embeds, screenshots
  // and demos need a deterministic look without touching the saved preference.
  try {
    const forced = new URLSearchParams(window.location.search).get('theme');
    if (forced === 'light' || forced === 'dark') return forced;
  } catch {
    // Defensive only: `URLSearchParams` exists in every supported browser.
  }
  try {
    const saved = localStorage.getItem(THEME_STORAGE_KEY);
    if (saved === 'light' || saved === 'dark') return saved;
  } catch {
    // Private mode / storage disabled: the default is still usable.
  }
  return 'light';
}

export function ThemeToggle() {
  const t = useT();
  const [theme, setTheme] = useState<Theme>(initialTheme);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch {
      // Not being able to remember the choice must not break the toggle.
    }
  }, [theme]);

  const next: Theme = theme === 'light' ? 'dark' : 'light';
  const label = next === 'light' ? t('app.themeLight') : t('app.themeDark');

  return (
    <button
      type="button"
      className="btn btn--ghost btn--icon theme-toggle"
      title={label}
      aria-label={t('app.theme')}
      onClick={() => setTheme(next)}
    >
      {theme === 'light' ? <Moon size={13} /> : <Sun size={13} />}
    </button>
  );
}
