/**
 * Decides whether the editor may mount.
 *
 * The status is fetched once before the editor starts, so a gate-only visitor
 * does not fire a burst of API requests that all come back 403. When a cookie
 * expires mid-session the gate is shown as an overlay instead of unmounting the
 * editor: losing an unsaved canvas because a 12-hour cookie aged out would be a
 * worse failure than the expiry itself.
 */

import { useCallback, useEffect, useState } from 'react';
import { Loader2, ShieldAlert } from 'lucide-react';

import { App } from '@/App';
import { GateScreen } from '@/access/GateScreen';
import { useAccess } from '@/access/accessStore';
import { useT } from '@/i18n';

export function GateRoot() {
  const t = useT();
  const status = useAccess((state) => state.status);
  const loading = useAccess((state) => state.loading);
  const error = useAccess((state) => state.error);
  const load = useAccess((state) => state.load);
  const markVerified = useAccess((state) => state.markVerified);

  const [ready, setReady] = useState(false);
  const [forcedOpen, setForcedOpen] = useState(false);
  const [appMounted, setAppMounted] = useState(false);

  const bootstrap = useCallback(async () => {
    try {
      await load();
      setReady(true);
    } catch {
      // `load` already recorded the error; the screen below offers a retry.
    }
  }, [load]);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  useEffect(() => {
    const open = () => setForcedOpen(true);
    window.addEventListener('scaffold:gate-required', open);
    return () => window.removeEventListener('scaffold:gate-required', open);
  }, []);

  const open = forcedOpen || (status.required && !status.verified);

  useEffect(() => {
    if (ready && !open) setAppMounted(true);
  }, [ready, open]);

  const verified = () => {
    setForcedOpen(false);
    markVerified();
  };

  if (!ready) {
    return (
      <div className="gate-screen">
        <section className="gate-card gate-card--compact">
          {loading ? (
            <p className="gate-hint">
              <Loader2 size={14} className="spin" /> {t('gate.checking')}
            </p>
          ) : (
            <>
              <p className="gate-hint gate-hint--warn">
                <ShieldAlert size={14} /> {error || t('gate.failed')}
              </p>
              <button
                type="button"
                className="btn btn--primary gate-button"
                onClick={() => void bootstrap()}
              >
                {t('gate.retry')}
              </button>
            </>
          )}
        </section>
      </div>
    );
  }

  if (open) {
    return (
      <>
        {appMounted ? <App /> : null}
        <GateScreen overlay={appMounted} onVerified={verified} />
      </>
    );
  }

  return <App />;
}
