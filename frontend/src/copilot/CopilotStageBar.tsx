/**
 * The strip above the canvas: mode, write scope, and the layered stage stack.
 *
 * The stack is deliberately above the canvas rather than in the conversation:
 * it is an answer to "what is the AI doing right now", and a user watching the
 * canvas grow should not have to look away from it. The sidebar stays purely
 * conversational.
 */

import type { CSSProperties } from 'react';
import { BoxSelect, Brain, Check, CircleDashed, Loader2, X } from 'lucide-react';

import { useCopilot, type CopilotStage, type StageStatus } from '@/copilot/copilotStore';
import { useT } from '@/i18n';
import type { MessageKey } from '@/i18n/translations';
import { useWorkflow } from '@/store/workflowStore';

export const STAGE_TITLES: Record<string, MessageKey> = {
  insight: 'copilot.stageInsight',
  investigate: 'copilot.stageInvestigate',
  research: 'copilot.stageResearch',
  plan: 'copilot.stagePlan',
  edit: 'copilot.stageEdit',
  validate: 'copilot.stageValidate',
  test: 'copilot.stageTest',
  review: 'copilot.stageReview',
  done: 'copilot.stageDone',
};

function isBusy(status: StageStatus): boolean {
  return status === 'running' || status === 'progress';
}

function isQuiet(status: StageStatus): boolean {
  return (
    status === 'skipped' ||
    status === 'disabled' ||
    status === 'unavailable' ||
    status === 'empty' ||
    status === 'pending'
  );
}

export function CopilotStageBar() {
  const t = useT();
  const mode = useCopilot((state) => state.mode);
  const setMode = useCopilot((state) => state.setMode);
  const selecting = useCopilot((state) => state.selecting);
  const setSelecting = useCopilot((state) => state.setSelecting);
  const stages = useCopilot((state) => state.stages);
  const thinkingEnabled = useCopilot((state) => state.thinkingEnabled);
  const toggleThinking = useCopilot((state) => state.toggleThinking);
  const running = useCopilot((state) => state.running);
  const waiting = useCopilot((state) => state.waiting);
  const stop = useCopilot((state) => state.stop);
  const clearScope = useCopilot((state) => state.clearScope);
  const selection = useWorkflow((state) => state.copilotSelection);

  const count = selection.nodes.length;

  return (
    <div className={`copilot-bar${running || waiting ? ' copilot-bar--busy' : ''}`}>
      <div className="copilot-bar__controls">
        <div className="copilot-modes" role="group" aria-label={t('copilot.title')}>
          <button
            type="button"
            className={`copilot-mode${mode === 'auto' ? ' copilot-mode--active' : ''}`}
            title={t('copilot.modeAutoHint')}
            onClick={() => {
              setMode('auto');
              setSelecting(false);
            }}
          >
            {t('copilot.modeAuto')}
          </button>
          <button
            type="button"
            className={`copilot-mode${mode === 'confirm' ? ' copilot-mode--active' : ''}`}
            title={t('copilot.modeConfirmHint')}
            onClick={() => setMode('confirm')}
          >
            {t('copilot.modeConfirm')}
          </button>
          <button
            type="button"
            className={`copilot-mode copilot-mode--thinking${
              thinkingEnabled ? ' copilot-mode--active' : ''
            }`}
            title={t('copilot.thinkingHint')}
            aria-pressed={thinkingEnabled}
            onClick={toggleThinking}
          >
            <Brain size={11} />
            {t('copilot.thinkingMode')}
          </button>
        </div>

        <div className="copilot-scope">
          <span className="copilot-scope__label">
            {mode === 'auto'
              ? t('copilot.scopeWhole')
              : count > 0
                ? t('copilot.scopeSelected', { count })
                : t('copilot.scopeEmpty')}
          </span>
          {mode === 'confirm' ? (
            <>
              <button
                type="button"
                className={`copilot-scope__button${selecting ? ' copilot-scope__button--active' : ''}`}
                title={t('copilot.selectHint')}
                onClick={() => setSelecting(!selecting)}
              >
                <BoxSelect size={11} />
                {selecting ? t('copilot.selecting') : t('copilot.selectScope')}
              </button>
              {count > 0 ? (
                <button
                  type="button"
                  className="copilot-scope__clear"
                  onClick={clearScope}
                  title={t('copilot.clearScope')}
                >
                  <X size={10} />
                </button>
              ) : null}
            </>
          ) : null}
        </div>

        <div className="spacer" />

        {running ? (
          <button type="button" className="btn btn--ghost btn--sm" onClick={stop}>
            <Loader2 size={11} className="spin" /> {t('copilot.stop')}
          </button>
        ) : waiting ? (
          <span className="copilot-waiting">
            <CircleDashed size={11} /> {t('copilot.planReady')}
          </span>
        ) : null}
      </div>

      {stages.length > 0 ? (
        <StageStack stages={stages} />
      ) : selecting ? (
        <div className="copilot-select-hint">
          <BoxSelect size={11} /> {t('copilot.selectHint')}
        </div>
      ) : null}
    </div>
  );
}

function StageStack({ stages }: { stages: CopilotStage[] }) {
  const t = useT();
  // One card at a time. Stacking older stages looked like leftover UI: after a
  // turn finished, the old "designing a plan" card was still visible underneath
  // the new one. The run timeline keeps the full history; this strip answers
  // only "what is happening now".
  const visible = stages.slice(-1);
  const newest = visible[visible.length - 1];

  return (
    <div className="copilot-stack" aria-live="polite">
      {visible.map((stage, index) => {
        const depth = visible.length - 1 - index;
        const active = stage === newest;
        return (
          <div
            key={`${stage.id}-${index}`}
            className={[
              'copilot-stage',
              `copilot-stage--${stage.status}`,
              active ? 'copilot-stage--active' : '',
            ]
              .filter(Boolean)
              .join(' ')}
            style={{ '--depth': depth } as CSSProperties}
          >
            <span className="copilot-stage__icon">
              {isBusy(stage.status) ? (
                <Loader2 size={11} className="spin" />
              ) : stage.status === 'done' ? (
                <Check size={11} />
              ) : stage.status === 'failed' ? (
                <X size={11} />
              ) : (
                <CircleDashed size={11} />
              )}
            </span>
            <span className="copilot-stage__title">
              {STAGE_TITLES[stage.id] ? t(STAGE_TITLES[stage.id]!) : stage.id}
            </span>
            {stage.detail ? (
              <span
                className={`copilot-stage__detail${isQuiet(stage.status) ? ' copilot-stage__detail--quiet' : ''}`}
                title={stage.detail}
              >
                {stage.detail}
              </span>
            ) : null}
            {isQuiet(stage.status) ? (
              <span className="copilot-stage__tag">{t('copilot.stageSkipped')}</span>
            ) : stage.status === 'failed' ? (
              <span className="copilot-stage__tag copilot-stage__tag--failed">
                {t('copilot.stageFailed')}
              </span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
