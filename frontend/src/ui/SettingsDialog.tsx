/**
 * Runtime settings.
 *
 * Deliberately narrow: only the LLM connection is editable, because the other
 * settings (recursion limit, whether code nodes may run) change what a run is
 * allowed to do, and those should require editing a file rather than a stray
 * click. The dialog mirrors that boundary instead of inventing its own.
 *
 * "Test connection" is the feature that matters here — a misconfigured endpoint
 * is by far the most common reason the AI buttons appear to do nothing, and the
 * backend returns the provider's own error so the cause is visible.
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertCircle, Check, Loader2, PlugZap, RefreshCw } from 'lucide-react';

import { ApiError, api } from '@/api/client';
import { useT } from '@/i18n';
import { useWorkflow } from '@/store/workflowStore';
import { Dialog } from '@/ui/Dialogs';

type Settings = Awaited<ReturnType<typeof api.settings>>;
type TestResult = Awaited<ReturnType<typeof api.testLLM>>;

/** What the backend returns for a stored key, and what it ignores on write. */
const REDACTED = '***';

export function SettingsDialog({ onClose }: { onClose: () => void }) {
  const t = useT();
  const loadAIStatus = useWorkflow((state) => state.loadAIStatus);

  const [settings, setSettings] = useState<Settings | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [model, setModel] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [temperature, setTemperature] = useState(0);

  const [models, setModels] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [test, setTest] = useState<TestResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const current = await api.settings();
      setSettings(current);
      setBaseUrl(current.llm.base_url);
      setModel(current.llm.model);
      setApiKey('');
      setTemperature(current.llm.temperature);
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : t('settings.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    setSaving(true);
    setError(null);
    setTest(null);
    try {
      const updated = await api.updateLLM({
        base_url: baseUrl,
        model,
        // An untouched field must not overwrite the stored key; the backend
        // treats `***` as "keep what you have".
        api_key: apiKey,
        temperature,
      });
      setSettings(updated);
      setApiKey('');
      await loadAIStatus();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : t('settings.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    try {
      setTest(await api.testLLM());
    } catch (caught) {
      setTest({
        ok: false,
        detail: caught instanceof ApiError ? caught.message : t('settings.testFailed'),
      });
    } finally {
      setTesting(false);
    }
  };

  const fetchModels = async () => {
    setError(null);
    try {
      const response = await api.listModels();
      setModels(response.models);
      if (response.models.length === 0) {
        setError(t('settings.noModels'));
      }
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? `${caught.message} Type the model name instead.`
          : t('settings.listFailed'),
      );
    }
  };

  const keyIsStored = settings?.llm.api_key === REDACTED;

  return (
    <Dialog
      title={t('settings.title')}
      icon={<PlugZap size={14} className="dialog__icon" />}
      onClose={onClose}
      footer={
        <>
          <button type="button" className="btn btn--ghost" onClick={() => void runTest()} disabled={testing}>
            {testing ? <Loader2 size={12} className="spin" /> : <PlugZap size={12} />}
            {testing ? t('settings.testing') : t('settings.test')}
          </button>
          <div className="spacer" />
          <button type="button" className="btn" onClick={onClose}>
            {t('settings.close')}
          </button>
          <button type="button" className="btn btn--primary" onClick={() => void save()} disabled={saving || loading}>
            {saving ? <Loader2 size={12} className="spin" /> : <Check size={12} />}
            {saving ? t('settings.saving') : t('settings.save')}
          </button>
        </>
      }
    >
      {loading ? (
        <p className="field__hint">{t('settings.loading')}</p>
      ) : (
        <div className="settings">
          {error ? (
            <div className="callout callout--error">
              <AlertCircle size={13} className="callout__icon" />
              <span>{error}</span>
            </div>
          ) : null}

          {settings?.mock_mode ? (
            <div className="callout callout--warning">
              <AlertCircle size={13} className="callout__icon" />
              <span>{t('settings.mock')}</span>
            </div>
          ) : null}

          <div className="field">
            <label className="field__label" htmlFor="llm-base-url">
              {t('settings.baseUrl')}
            </label>
            <input
              id="llm-base-url"
              className="input mono"
              value={baseUrl}
              onChange={(event) => setBaseUrl(event.target.value)}
              placeholder="https://api.deepseek.com/v1"
              spellCheck={false}
            />
            <p className="field__hint">{t('settings.baseUrlHint', { v1: '/v1' })}</p>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="llm-model">
              {t('settings.model')}
            </label>
            <div className="row row--tight">
              <input
                id="llm-model"
                className="input mono grow"
                value={model}
                onChange={(event) => setModel(event.target.value)}
                placeholder="deepseek-chat"
                spellCheck={false}
                list="llm-models"
              />
              <datalist id="llm-models">
                {models.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
              <button
                type="button"
                className="btn btn--icon"
                title={t('settings.fetchModels')}
                onClick={() => void fetchModels()}
              >
                <RefreshCw size={12} />
              </button>
            </div>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="llm-key">
              {t('settings.apiKey')}
            </label>
            <input
              id="llm-key"
              className="input mono"
              type="password"
              value={apiKey}
              onChange={(event) => setApiKey(event.target.value)}
              placeholder={keyIsStored ? t('settings.apiKeyStored') : 'sk-…'}
              autoComplete="off"
              spellCheck={false}
            />
            <p className="field__hint">{t('settings.apiKeyHint', { env: '.env' })}</p>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="llm-temperature">
              {t('settings.temperature')}
            </label>
            <input
              id="llm-temperature"
              className="input"
              type="number"
              min={0}
              max={2}
              step={0.1}
              value={temperature}
              onChange={(event) => setTemperature(Number(event.target.value))}
            />
            <p className="field__hint">{t('settings.temperatureHint')}</p>
          </div>

          {settings ? (
            <div className="settings__meta">
              <div className="row row--between">
                <span className="muted">{t('settings.aiFeatures')}</span>
                <span className={`badge badge--${settings.ai_enabled ? 'success' : 'warning'}`}>
                  {settings.ai_enabled ? t('settings.enabled') : t('settings.disabled')}
                </span>
              </div>
              <div className="row row--between">
                <span className="muted">{t('settings.codeNodes')}</span>
                <span className={`badge badge--${settings.code_node_enabled ? 'success' : 'warning'}`}>
                  {settings.code_node_enabled ? t('settings.enabled') : t('settings.disabled')}
                </span>
              </div>
              <div className="row row--between">
                <span className="muted">{t('settings.recursionLimit')}</span>
                <span className="mono">{settings.recursion_limit}</span>
              </div>
              <p className="field__hint">{t('settings.meta')}</p>
            </div>
          ) : null}

          {test ? (
            test.ok ? (
              <div className="callout callout--success">
                <Check size={13} className="callout__icon" />
                <span>
                  {t('settings.connected', {
                    model: test.model ?? '',
                    reply: test.reply ?? '',
                  })}
                </span>
              </div>
            ) : (
              <div className="callout callout--error">
                <AlertCircle size={13} className="callout__icon" />
                <span>
                  {test.detail}
                  {test.hint ? <><br />{test.hint}</> : null}
                </span>
              </div>
            )
          ) : null}
        </div>
      )}
    </Dialog>
  );
}
