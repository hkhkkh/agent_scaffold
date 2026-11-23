/**
 * Application shell.
 *
 * Wires the four regions (palette, canvas, inspector/run panel, header) to the
 * store, and owns the two pieces of state that genuinely belong above the
 * components: **the live SSE stream** and **which dialog is open**.
 *
 * The event bridge below deserves a note. `BaseNode` dispatches plain DOM
 * CustomEvents instead of importing the store, so that a node card stays a
 * presentational component with no coupling to editor state. That trade is only
 * worth it if someone subscribes — which happens here, once.
 */

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  AlertCircle,
  Bot,
  Database,
  FolderOpen,
  History,
  Keyboard,
  Languages,
  LayoutGrid,
  Loader2,
  UnfoldHorizontal,
  PanelLeftClose,
  PanelLeftOpen,
  PanelRight,
  PanelRightClose,
  PanelRightOpen,
  Play,
  Redo2,
  Save,
  Settings,
  Undo2,
  X,
} from 'lucide-react';

import { useAccess } from '@/access/accessStore';
import { PublicDemoBanner } from '@/access/PublicDemoBanner';
import { beginRun, closeRunStream, installRunBridge } from '@/run/runController';
import { RepairGraphDialog } from '@/ai/RepairGraphDialog';
import { Canvas } from '@/canvas/Canvas';
import { CopilotSidebar } from '@/copilot/CopilotSidebar';
import { CopilotStageBar } from '@/copilot/CopilotStageBar';
import { useCopilot } from '@/copilot/copilotStore';
import { DbPanel } from '@/db/DbPanel';
import { NodePalette } from '@/canvas/NodePalette';import { LOCALE_LABELS, SUPPORTED_LOCALES, useLocale, useT } from '@/i18n';
import { Inspector } from '@/inspector/Inspector';
import { HumanInputDialog } from '@/run/HumanInputDialog';
import { RunDialog, RunPanel } from '@/run/RunPanel';
import {
  canRedo,
  canUndo,
  selectErrors,
  selectWarnings,
  useWorkflow,
  type CanvasNode,
} from '@/store/workflowStore';
import { ConfirmDialog, Dialog } from '@/ui/Dialogs';
import { SettingsDialog } from '@/ui/SettingsDialog';
import { ThemeToggle } from '@/ui/ThemeToggle';
import { WorkflowBrowser } from '@/ui/WorkflowBrowser';
import { UseView } from '@/use/UseView';
import { VersionHistoryDialog } from '@/versions/VersionHistoryDialog';

type DialogKind =
  | 'none'
  | 'open'
  | 'repair'
  | 'settings'
  | 'run'
  | 'versions';

const LEFT_PANEL_KEY = 'scaffold.panel.left';
const RIGHT_PANEL_KEY = 'scaffold.panel.right';

/**
 * Whether a side panel starts open.
 *
 * `localStorage` can throw (private windows, blocked storage). A panel that
 * fails to remember its state is a minor annoyance; an editor that fails to
 * boot is not, so the fallback is "open" and the error is swallowed.
 */
function readPanelPreference(key: string): boolean {
  try {
    return localStorage.getItem(key) !== 'closed';
  } catch {
    return true;
  }
}

function writePanelPreference(key: string, open: boolean): void {
  try {
    localStorage.setItem(key, open ? 'open' : 'closed');
  } catch {
    // See above: persistence is a convenience, not a requirement.
  }
}

export function App() {
  const name = useWorkflow((state) => state.name);
  const dirty = useWorkflow((state) => state.dirty);
  const saving = useWorkflow((state) => state.saving);
  const error = useWorkflow((state) => state.error);
  const validating = useWorkflow((state) => state.validating);
  const lastSavedAt = useWorkflow((state) => state.lastSavedAt);
  const runId = useWorkflow((state) => state.runId);
  const runStatus = useWorkflow((state) => state.runStatus);
  const nodeTypes = useWorkflow((state) => state.nodeTypes);
  // Subscribe to the stable `issues` array, then derive: a selector that builds
  // a new array each call would re-render forever (see selectErrors).
  const issues = useWorkflow((state) => state.issues);
  const nodes = useWorkflow((state) => state.nodes);
  const undoable = useWorkflow(canUndo);
  const redoable = useWorkflow(canRedo);
  const capabilities = useAccess((state) => state.status.capabilities);
  const readOnly = useMemo(
    () =>
      capabilities.public_demo &&
      nodes.some((node) => node.data.spec.type === 'code'),
    [capabilities.public_demo, nodes],
  );

  const loadCatalogue = useWorkflow((state) => state.loadCatalogue);
  const loadAIStatus = useWorkflow((state) => state.loadAIStatus);
  const validate = useWorkflow((state) => state.validate);
  const setName = useWorkflow((state) => state.setName);
  const save = useWorkflow((state) => state.save);
  const undo = useWorkflow((state) => state.undo);
  const redo = useWorkflow((state) => state.redo);
  const reset = useWorkflow((state) => state.reset);
  const setError = useWorkflow((state) => state.setError);
  const tidyLayout = useWorkflow((state) => state.tidyLayout);

  // Which of the two faces of the app is showing: the canvas that makes an
  // agent, or the plain view that uses it. Shell state, not document state.
  const [view, setView] = useState<'build' | 'use'>('build');
  const [dialog, setDialog] = useState<DialogKind>('none');
  // Native window.confirm would look out of place next to the rest of the
  // product's dialogs (and block the renderer while it is open), so the
  // discard question is an in-app confirm like every other destructive act.
  const [pendingNew, setPendingNew] = useState(false);
  const [tab, setTab] = useState<'ai' | 'inspector' | 'run' | 'db'>('ai');
  const [starting, setStarting] = useState(false);
  // Side panels collapse to a slim rail. Persisted per browser so the editor
  // opens the way the user left it.
  const [leftOpen, setLeftOpen] = useState(() =>
    readPanelPreference(LEFT_PANEL_KEY),
  );
  const [rightOpen, setRightOpen] = useState(() =>
    readPanelPreference(RIGHT_PANEL_KEY),
  );
  const [tidying, setTidying] = useState(false);

  const t = useT();
  const locale = useLocale((state) => state.locale);
  const setLocale = useLocale((state) => state.setLocale);

  const errors = useMemo(() => selectErrors(issues), [issues]);
  const warnings = useMemo(() => selectWarnings(issues), [issues]);

  /** The language the toggle would switch to. Two options, so it just cycles. */
  const nextLocale = useMemo(() => {
    const index = SUPPORTED_LOCALES.indexOf(locale);
    return SUPPORTED_LOCALES[(index + 1) % SUPPORTED_LOCALES.length] ?? 'en';
  }, [locale]);

  const catalogueLoaded = Object.keys(nodeTypes).length > 0;

  // -------------------------------------------------------------------------
  // Bootstrap
  // -------------------------------------------------------------------------

  useEffect(() => {
    void loadCatalogue();
    void loadAIStatus();
    // Re-validate too: issue *messages* are prose, so they are generated by the
    // backend in the requested language (unlike node/port ids, which never
    // change). Without this the badges keep the previous language while the
    // rest of the chrome switches.
    void validate();
  }, [loadCatalogue, loadAIStatus, validate, locale]);

  // The document language drives hyphenation rules and screen-reader
  // pronunciation, so it is kept in step with the interface language.
  useEffect(() => {
    document.documentElement.lang = locale === 'zh' ? 'zh-CN' : 'en';
  }, [locale]);

  // -------------------------------------------------------------------------
  // Canvas event bridge
  // -------------------------------------------------------------------------

  useEffect(() => {
    const onUpdate = (event: Event) => {
      const detail = (event as CustomEvent<{ id: string; patch: Record<string, unknown> }>).detail;
      if (!detail?.id) return;
      useWorkflow.getState().updateNode(detail.id, detail.patch);
    };
    const onSelect = (event: Event) => {
      const detail = (event as CustomEvent<{ id: string }>).detail;
      if (!detail?.id) return;
      useWorkflow.getState().selectNode(detail.id);
      setTab('inspector');
    };
    const onVersions = () => setDialog('versions');

    window.addEventListener('scaffold:update-node', onUpdate);
    window.addEventListener('scaffold:select-node', onSelect);
    window.addEventListener('scaffold:open-versions', onVersions);
    return () => {
      window.removeEventListener('scaffold:update-node', onUpdate);
      window.removeEventListener('scaffold:select-node', onSelect);
      window.removeEventListener('scaffold:open-versions', onVersions);
    };
  }, []);

  // -------------------------------------------------------------------------
  // Run lifecycle
  // -------------------------------------------------------------------------

  // The stream must not outlive the app: a dangling reader keeps the connection
  // open and React would warn about the state update it triggers on teardown.
  useEffect(() => () => closeRunStream(), []);

  // A human input node was answered: start reading the run's events again.
  useEffect(() => installRunBridge(), []);

  // AI work is on demand, from the right-hand assistant: nothing runs while the
  // user is editing unless they asked in the panel, so the auto-fill engine is
  // no longer mounted (its mode toggle was retired from the header too).

  useEffect(() => {
    writePanelPreference(LEFT_PANEL_KEY, leftOpen);
  }, [leftOpen]);

  useEffect(() => {
    writePanelPreference(RIGHT_PANEL_KEY, rightOpen);
  }, [rightOpen]);

  const handleTidyLayout = useCallback(
    async (perBand = 6) => {
      setTidying(true);
      try {
        // 6 columns is the compact, banded layout; 0 lays the graph out as one
        // straight line so dependency order reads left to right.
        await tidyLayout(perBand);
      } finally {
        setTidying(false);
      }
    },
    [tidyLayout],
  );

  const handleRun = useCallback(
    async (inputs: Record<string, unknown>) => {
      setDialog('none');
      setTab('run');
      setStarting(true);
      setError(null);

      // On failure `beginRun` returns null: `startRun` has recorded the reason
      // and the run panel shows it, so there is no second error to raise here.
      await beginRun(inputs);
      setStarting(false);
    },
    [setError],
  );

  // -------------------------------------------------------------------------
  // Shortcuts
  // -------------------------------------------------------------------------

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const mod = event.metaKey || event.ctrlKey;
      if (!mod) return;

      const target = event.target as HTMLElement | null;
      const typing =
        target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
      const key = event.key.toLowerCase();

      if (key === 's') {
        event.preventDefault();
        void save();
        return;
      }
      if (key === 'k') {
        event.preventDefault();
        // One AI entry point: the right-hand assistant. Ctrl+K opens it and
        // puts the cursor in its composer instead of opening a second dialog.
        setRightOpen(true);
        setTab('ai');
        window.setTimeout(() => {
          document.querySelector<HTMLTextAreaElement>('.copilot__input')?.focus();
        }, 0);
        return;
      }
      if (key === 'z') {
        // Inside a text field the browser's own undo is what the user means.
        if (typing) return;
        event.preventDefault();
        if (event.shiftKey) redo();
        else undo();
      }
    };

    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [catalogueLoaded, redo, save, undo]);

  const running = runStatus === 'running';
  const stepCount = useWorkflow((state) => Object.keys(state.stepByNode).length);

  // Computed only when the dialog is opened: reading node positions on every
  // drag would re-render the whole shell for a value nobody is looking at.
  const initialRunInputs = useMemo(
    () => (dialog === 'run' ? runInputTemplate(useWorkflow.getState().nodes) : '{}'),
    [dialog],
  );

  return (
    <div className="app">
      {/* ------------------------------------------------------------------ */}
      {/* Header                                                             */}
      {/* ------------------------------------------------------------------ */}

      <header className="app-header">
        <div className="brand" title={t('app.name')}>
          <Bot size={16} />
          <span className="brand__name">{t('app.name')}</span>
        </div>
        <PublicDemoBanner />

        <div className="header__divider" />

        <div className="tabs">
          <button
            type="button"
            className={`tab${view === 'build' ? ' tab--active' : ''}`}
            onClick={() => setView('build')}
          >
            {t('use.edit')}
          </button>
          <button
            type="button"
            className={`tab${view === 'use' ? ' tab--active' : ''}`}
            onClick={() => setView('use')}
          >
            {t('use.use')}
          </button>
        </div>

        {/* Everything in this branch *edits* the workflow. The use view is for
            people who did not make it, so none of it belongs on screen there. */}
        {view === 'build' ? (
          <>
            <div className="header__divider" />

            <input
              className="header__name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder={t('app.untitled')}
              aria-label={t('app.workflowName')}
            />

            <SaveState
              dirty={dirty}
              saving={saving}
              validating={validating}
              lastSavedAt={lastSavedAt}
            />

            <div className="spacer" />

            {errors.length > 0 ? (
              <span className="badge badge--danger" title={errors[0]?.message}>
                <AlertCircle size={10} /> {errors.length}
              </span>
            ) : null}
            {warnings.length > 0 ? (
              <span className="badge badge--warning" title={warnings[0]?.message}>
                {warnings.length}
              </span>
            ) : null}

            <button
              type="button"
              className="btn btn--ghost btn--icon"
              title={t('app.new')}
              onClick={() => {
                if (dirty) {
                  setPendingNew(true);
                  return;
                }
                reset();
              }}
            >
              <X size={13} />
            </button>

            <button
              type="button"
              className="btn btn--ghost btn--icon"
              title={t('app.open')}
              onClick={() => setDialog('open')}
            >
              <FolderOpen size={13} />
            </button>

            <button
              type="button"
              className="btn btn--ghost btn--icon"
              title={t('app.versions')}
              onClick={() => setDialog('versions')}
            >
              <History size={13} />
            </button>

            <button
              type="button"
              className="btn btn--ghost btn--icon"
              title={t('app.undo')}
              disabled={!undoable}
              onClick={undo}
            >
              <Undo2 size={13} />
            </button>
            <button
              type="button"
              className="btn btn--ghost btn--icon"
              title={t('app.redo')}
              disabled={!redoable}
              onClick={redo}
            >
              <Redo2 size={13} />
            </button>

            {view === 'build' ? (
              <>
                <button
                  type="button"
                  className="btn btn--ghost btn--icon"
                  title={t('app.tidyLayoutHint')}
                  disabled={!nodes.length || tidying}
                  onClick={() => void handleTidyLayout(6)}
                >
                  {tidying ? (
                    <Loader2 size={13} className="spin" />
                  ) : (
                    <LayoutGrid size={13} />
                  )}
                </button>
                <button
                  type="button"
                  className="btn btn--ghost btn--icon"
                  title={t('app.expandLayoutHint')}
                  disabled={!nodes.length || tidying}
                  onClick={() => void handleTidyLayout(0)}
                >
                  {tidying ? (
                    <Loader2 size={13} className="spin" />
                  ) : (
                    <UnfoldHorizontal size={13} />
                  )}
                </button>
              </>
            ) : null}

            <div className="header__divider" />

            <button
              type="button"
              className="btn"
              title={readOnly ? t('app.readOnlyCode') : undefined}
              onClick={() => void save()}
              disabled={saving || !catalogueLoaded || readOnly}
            >
              {saving ? <Loader2 size={12} className="spin" /> : <Save size={12} />}{' '}
              {t('app.save')}
            </button>

            {capabilities.settings ? (
              <button
                type="button"
                className="btn btn--ghost btn--icon"
                title={t('app.settings')}
                onClick={() => setDialog('settings')}
              >
                <Settings size={13} />
              </button>
            ) : null}
          </>
        ) : (
          <div className="spacer" />
        )}

        <ThemeToggle />

        <button
          type="button"
          className="btn btn--ghost header__lang"
          // Shows the language you would *get*, which is the useful direction for
          // a toggle. With only two options a dropdown would be overkill.
          title={t('app.switchLanguage')}
          aria-label={`${t('app.language')}: ${LOCALE_LABELS[locale]}`}
          onClick={() => setLocale(nextLocale)}
        >
          <Languages size={13} />
          {LOCALE_LABELS[nextLocale]}
        </button>

        {/* The use view has its own inputs and its own run button, so this one
            would be a second, differently-wired way to start the same run. */}
        {view === 'build' ? (
          <button
            type="button"
            className="btn btn--primary"
            title={
              readOnly
                ? t('app.readOnlyCode')
                : errors.length > 0
                  ? t('app.runBlocked')
                  : undefined
            }
            // An invalid graph cannot run, and a second click while one is in
            // flight would queue another. Both are stopped here.
            disabled={
              !catalogueLoaded || starting || readOnly || running || errors.length > 0
            }
            onClick={() => setDialog('run')}
          >
            {starting ? <Loader2 size={12} className="spin" /> : <Play size={12} />}
            {running ? t('app.running') : t('app.run')}
          </button>
        ) : null}
      </header>

      {/* ------------------------------------------------------------------ */}
      {/* Body                                                               */}
      {/* ------------------------------------------------------------------ */}

      <div className="app-body">
        {view === 'build' ? (
          <aside
            className={`app-side app-side--left${
              leftOpen ? '' : ' app-side--collapsed'
            }`}
          >
            {leftOpen ? (
              <>
                <div className="app-side__head">
                  <span className="app-side__title">{t('app.palette')}</span>
                  <button
                    type="button"
                    className="btn btn--ghost btn--icon btn--sm"
                    title={t('app.collapseLeft')}
                    onClick={() => setLeftOpen(false)}
                  >
                    <PanelLeftClose size={12} />
                  </button>
                </div>
                {catalogueLoaded ? (
                  <NodePalette />
                ) : (
                  <div className="palette">
                    <p className="field__hint">
                      <Loader2 size={12} className="spin" /> {t('app.loadingNodes')}
                    </p>
                  </div>
                )}
              </>
            ) : (
              <button
                type="button"
                className="app-side__rail"
                title={t('app.expandLeft')}
                onClick={() => setLeftOpen(true)}
              >
                <PanelLeftOpen size={13} />
                <span className="app-side__rail-label">{t('app.palette')}</span>
              </button>
            )}
          </aside>
        ) : null}

        <main className="app-main">
          {view === 'build' ? (
            <div className="canvas-column">
              <CopilotStageBar />
              <Canvas
                onRepair={() => setDialog('repair')}
                onScenario={(prompt) => {
                  // One AI surface. A scenario is the first message to the
                  // right-hand assistant, not a second generator with its own
                  // dialog to open and close.
                  setRightOpen(true);
                  setTab('ai');
                  useCopilot.getState().send(prompt);
                }}
                onOpenRun={() => setTab('run')}
              />
            </div>
          ) : (
            <UseView />
          )}

          {readOnly ? (
            <div className="banner banner--warning">
              <AlertCircle size={13} />
              <span>{t('app.readOnlyCode')}</span>
            </div>
          ) : null}

          {error ? (
            <div className="banner banner--error">
              <AlertCircle size={13} />
              <span className="truncate">{error}</span>
              <div className="spacer" />
              <button
                type="button"
                className="btn btn--ghost btn--icon btn--sm"
                onClick={() => setError(null)}
                title={t('app.dismiss')}
              >
                <X size={11} />
              </button>
            </div>
          ) : null}
        </main>

        {view === 'build' ? (
          <aside
            className={`app-side${rightOpen ? '' : ' app-side--collapsed'}`}
          >
            {rightOpen ? (
              <>
                <div className="app-side__tabs">
                  <div className="tabs">
                    <button
                      type="button"
                      className={`tab${tab === 'ai' ? ' tab--active' : ''}`}
                      onClick={() => setTab('ai')}
                    >
                      <Bot size={11} /> {t('app.tabAI')}
                    </button>
                    <button
                      type="button"
                      className={`tab${tab === 'inspector' ? ' tab--active' : ''}`}
                      onClick={() => setTab('inspector')}
                    >
                      <PanelRight size={11} /> {t('app.tabNode')}
                    </button>
                    <button
                      type="button"
                      className={`tab${tab === 'run' ? ' tab--active' : ''}`}
                      onClick={() => setTab('run')}
                    >
                      <Play size={11} /> {t('app.tabRun')}
                      {stepCount > 0 ? <span className="tab__count">{stepCount}</span> : null}
                    </button>
                    {/* Beside the canvas rather than inside a dialog, because the
                        point of this tab is to be watchable *while* the graph runs
                        — a panel you have to open, and that covers the run you are
                        watching, would answer the question too late. */}
                    <button
                      type="button"
                      className={`tab${tab === 'db' ? ' tab--active' : ''}`}
                      onClick={() => setTab('db')}
                    >
                      <Database size={11} /> {t('app.tabDb')}
                    </button>
                  </div>
                  <button
                    type="button"
                    className="btn btn--ghost btn--icon btn--sm"
                    title={t('app.collapseRight')}
                    onClick={() => setRightOpen(false)}
                  >
                    <PanelRightClose size={12} />
                  </button>
                </div>

                <div className="app-side__body">
                  {tab === 'ai' ? (
                    <CopilotSidebar />
                  ) : tab === 'inspector' ? (
                    <Inspector />
                  ) : tab === 'db' ? (
                    <DbPanel />
                  ) : (
                    <RunPanel onRun={() => setDialog('run')} />
                  )}
                </div>
              </>
            ) : (
              <button
                type="button"
                className="app-side__rail"
                title={t('app.expandRight')}
                onClick={() => setRightOpen(true)}
              >
                <PanelRightOpen size={13} />
                <span className="app-side__rail-label">
                  {t(
                    tab === 'ai'
                      ? 'app.tabAI'
                      : tab === 'inspector'
                        ? 'app.tabNode'
                        : tab === 'run'
                          ? 'app.tabRun'
                          : 'app.tabDb',
                  )}
                </span>
              </button>
            )}
          </aside>
        ) : null}
      </div>

      {/* ------------------------------------------------------------------ */}
      {/* Dialogs                                                            */}
      {/* ------------------------------------------------------------------ */}

      {pendingNew ? (
        <ConfirmDialog
          title={t('app.discardNewTitle')}
          message={t('app.discardNewMessage')}
          confirmLabel={t('app.discardNewConfirm')}
          danger
          onConfirm={() => {
            setPendingNew(false);
            reset();
          }}
          onCancel={() => setPendingNew(false)}
        />
      ) : null}

      {dialog === 'open' ? <WorkflowBrowser onClose={() => setDialog('none')} /> : null}
      {dialog === 'versions' ? (
        <VersionHistoryDialog onClose={() => setDialog('none')} />
      ) : null}
      {dialog === 'repair' ? <RepairGraphDialog onClose={() => setDialog('none')} /> : null}
      {dialog === 'settings' && capabilities.settings ? (
        <SettingsDialog onClose={() => setDialog('none')} />
      ) : null}
      {dialog === 'run' ? (
        /* The run form used to be rendered as a bare block after the app shell:
           no backdrop, no title, no close button, and the canvas stayed visible
           behind it. It is a modal, so it gets the shared dialog shell like every
           other modal in the app. */
        <Dialog
          title={t('runDialog.title')}
          icon={<Play size={14} className="dialog__icon" />}
          onClose={() => setDialog('none')}
        >
          <RunDialog
            initialText={initialRunInputs}
            busy={starting}
            onRun={(inputs) => void handleRun(inputs)}
            onClose={() => setDialog('none')}
          />
        </Dialog>
      ) : null}

      {/* The use view asks the same question inline, inside the conversation —
          a modal covering the reasoning the user is watching reads as an
          interruption rather than a turn. */}
      {view === 'build' ? <HumanInputDialog /> : null}

      {/* The hint bar explains canvas gestures. The use view has no canvas, so
          the same bar would be telling an end user to drag nodes that are not
          on their screen. */}
      {view === 'build' ? (
        <footer className="app-hint">
          <Keyboard size={11} />
          <span>{t('app.hint')}</span>
          {runId && running ? (
            <span className="mono dim">
              {t('app.runMeta', { id: runId.slice(0, 8), status: runStatus ?? '' })}
            </span>
          ) : null}
        </footer>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Pieces
// ---------------------------------------------------------------------------

function SaveState({
  dirty,
  saving,
  validating,
  lastSavedAt,
}: {
  dirty: boolean;
  saving: boolean;
  validating: boolean;
  lastSavedAt: string | null;
}) {
  const t = useT();
  const locale = useLocale((state) => state.locale);

  if (saving) {
    return (
      <span className="header__state">
        <Loader2 size={11} className="spin" /> {t('app.saving')}
      </span>
    );
  }
  if (validating) {
    return (
      <span className="header__state">
        <Loader2 size={11} className="spin" /> {t('app.checking')}
      </span>
    );
  }
  if (dirty) return <span className="header__state header__state--dirty">{t('app.unsaved')}</span>;
  if (lastSavedAt) {
    // The timestamp format is part of the language, not just the words around it.
    const time = new Date(lastSavedAt).toLocaleTimeString(
      locale === 'zh' ? 'zh-CN' : 'en-US',
    );
    return <span className="header__state">{t('app.savedAt', { time })}</span>;
  }
  return null;
}

/**
 * Prefill the run dialog from the Start node's declared inputs.
 *
 * Falls back to `{}` when there is no Start node — the run will fail validation
 * anyway, and an empty object is a clearer starting point than a guess.
 */
function runInputTemplate(nodes: CanvasNode[]): string {
  const start = nodes.find((node) => node.data.spec.type === 'start');
  const declared = start?.data.spec.config.input_schema;
  if (declared && typeof declared === 'object' && !Array.isArray(declared)) {
    return JSON.stringify(declared, null, 2);
  }
  return '{}';
}
