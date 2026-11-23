/**
 * Cloudflare Turnstile loader.
 *
 * The widget is a progressive enhancement: if the script is blocked (mainland
 * China is the reason this exists) the gate must still be usable through the
 * proof-of-work fallback. Every failure path therefore resolves rather than
 * rejecting into the caller's render tree.
 */

const SCRIPT_SRC =
  'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';

interface TurnstileApi {
  render: (
    container: HTMLElement,
    options: {
      sitekey: string;
      action?: string;
      theme?: 'light' | 'dark' | 'auto';
      callback?: (token: string) => void;
      'error-callback'?: () => void;
      'expired-callback'?: () => void;
    },
  ) => string;
  remove: (widgetId?: string) => void;
}

declare global {
  interface Window {
    turnstile?: TurnstileApi;
  }
}

let loader: Promise<boolean> | null = null;

export function loadTurnstile(timeoutMs = 8_000): Promise<boolean> {
  if (typeof window === 'undefined') return Promise.resolve(false);
  if (window.turnstile) return Promise.resolve(true);
  if (loader) return loader;

  loader = new Promise<boolean>((resolve) => {
    const existing = document.querySelector<HTMLScriptElement>(
      `script[src="${SCRIPT_SRC}"]`,
    );
    let settled = false;

    const finish = (ok: boolean) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      resolve(ok);
    };

    const timer = window.setTimeout(() => finish(false), timeoutMs);
    const script = existing ?? document.createElement('script');
    if (!existing) {
      script.src = SCRIPT_SRC;
      script.async = true;
      script.defer = true;
      script.onload = () => finish(Boolean(window.turnstile));
      script.onerror = () => finish(false);
      document.head.appendChild(script);
    } else {
      script.addEventListener('load', () => finish(Boolean(window.turnstile)));
      script.addEventListener('error', () => finish(false));
    }
  });

  return loader;
}

export function renderTurnstile(
  container: HTMLElement,
  siteKey: string,
  onToken: (token: string) => void,
  onError: () => void,
): string | null {
  if (!window.turnstile) return null;
  try {
    return window.turnstile.render(container, {
      sitekey: siteKey,
      action: 'scaffold-gate',
      theme: 'dark',
      callback: onToken,
      'error-callback': onError,
      'expired-callback': onError,
    });
  } catch {
    onError();
    return null;
  }
}

export function removeTurnstile(widgetId: string | null): void {
  if (!widgetId || !window.turnstile) return;
  try {
    window.turnstile.remove(widgetId);
  } catch {
    // Removing a widget that already expired is not an error worth surfacing.
  }
}
