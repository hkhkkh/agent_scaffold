/**
 * The human-verification screen.
 *
 * Turnstile is attempted first when configured. Because this deployment is used
 * from mainland China, the proof-of-work path is always one click away and is
 * selected automatically when the script fails to load.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, CheckCircle2, Loader2, ShieldCheck } from 'lucide-react';

import { useT } from '@/i18n';
import { useAccess } from '@/access/accessStore';
import { solvePow } from '@/access/pow';
import { loadTurnstile, removeTurnstile, renderTurnstile } from '@/access/turnstile';

interface Props {
  overlay?: boolean;
  onVerified?: () => void;
}

function detailOf(payload: unknown): string {
  if (typeof payload === 'object' && payload !== null) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
  }
  return '';
}

export function GateScreen({ overlay = false, onVerified }: Props) {
  const t = useT();
  const status = useAccess((state) => state.status);
  const markVerified = useAccess((state) => state.markVerified);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [progress, setProgress] = useState(0);
  const [turnstileReady, setTurnstileReady] = useState(false);
  const [turnstileFailed, setTurnstileFailed] = useState(false);
  const widgetRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const finish = useCallback(() => {
    markVerified();
    onVerified?.();
  }, [markVerified, onVerified]);

  const verify = useCallback(
    async (body: Record<string, unknown>) => {
      setBusy(true);
      setError('');
      try {
        const response = await fetch('/api/access/verify', {
          method: 'POST',
          credentials: 'same-origin',
          headers: {
            'Content-Type': 'application/json',
            'X-Scaffold-Client': 'web',
          },
          body: JSON.stringify(body),
        });
        const payload: unknown = await response.json().catch(() => null);
        if (!response.ok) {
          throw new Error(detailOf(payload) || t('gate.failed'));
        }
        finish();
      } catch (caught) {
        setError(caught instanceof Error ? caught.message : String(caught));
      } finally {
        setBusy(false);
      }
    },
    [finish, t],
  );

  // Load Turnstile only when the deployment configured a site key. A blocked
  // script is a normal outcome here, not an exception to propagate.
  useEffect(() => {
    if (!status.turnstile_site_key) return;
    let cancelled = false;
    void loadTurnstile().then((ready) => {
      if (cancelled) return;
      setTurnstileReady(ready);
      setTurnstileFailed(!ready);
    });
    return () => {
      cancelled = true;
    };
  }, [status.turnstile_site_key]);

  useEffect(() => {
    if (!turnstileReady || !widgetRef.current || !status.turnstile_site_key) return;
    const id = renderTurnstile(
      widgetRef.current,
      status.turnstile_site_key,
      (token) => void verify({ turnstile_token: token }),
      () => setTurnstileFailed(true),
    );
    return () => removeTurnstile(id);
  }, [turnstileReady, status.turnstile_site_key, verify]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const startPow = async () => {
    setBusy(true);
    setError('');
    setProgress(0);
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      const response = await fetch('/api/access/challenge', {
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'X-Scaffold-Client': 'web' },
      });
      const payload = (await response.json()) as {
        challenge?: string;
        nonce?: string;
        difficulty?: number;
        verified?: boolean;
        detail?: string;
      };
      if (!response.ok) throw new Error(payload.detail || t('gate.failed'));
      if (payload.verified) {
        finish();
        return;
      }
      if (!payload.challenge || !payload.nonce || !payload.difficulty) {
        throw new Error(t('gate.failed'));
      }
      const solution = await solvePow(payload.nonce, payload.difficulty, {
        signal: controller.signal,
        onProgress: setProgress,
      });
      await verify({ challenge: payload.challenge, solution });
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === 'AbortError') return;
      setError(caught instanceof Error ? caught.message : String(caught));
      setBusy(false);
    }
  };

  const showTurnstile = Boolean(status.turnstile_site_key) && !turnstileFailed;

  return (
    <div className={`gate-screen${overlay ? ' gate-screen--overlay' : ''}`}>
      <section className="gate-card" role="dialog" aria-modal="true">
        <div className="gate-brand">
          <span className="gate-brand__icon">
            <ShieldCheck size={20} />
          </span>
          <div>
            <h1 className="gate-title">{t('gate.title')}</h1>
            <p className="gate-subtitle">{t('gate.subtitle')}</p>
          </div>
        </div>

        {status.public_demo ? (
          <p className="callout callout--info gate-note">{t('gate.demoNote')}</p>
        ) : null}

        {showTurnstile ? (
          <div className="gate-turnstile">
            <div ref={widgetRef} />
            {!turnstileReady ? (
              <p className="gate-hint">
                <Loader2 size={12} className="spin" /> {t('gate.turnstileLoading')}
              </p>
            ) : null}
          </div>
        ) : null}

        {turnstileFailed ? (
          <p className="gate-hint gate-hint--warn">
            <AlertCircle size={12} /> {t('gate.turnstileUnavailable')}
          </p>
        ) : null}

        <button
          type="button"
          className="btn btn--primary gate-button"
          onClick={() => void startPow()}
          disabled={busy}
        >
          {busy ? <Loader2 size={13} className="spin" /> : <CheckCircle2 size={13} />}
          {busy ? t('gate.working', { attempts: progress.toLocaleString() }) : t('gate.continue')}
        </button>

        <p className="gate-hint">
          {t('gate.fallbackHint')}
        </p>

        {error ? (
          <p className="callout callout--error gate-error">
            <AlertCircle size={12} /> {error}
          </p>
        ) : null}

        <p className="gate-footnote">{t('gate.footnote')}</p>
      </section>
    </div>
  );
}
