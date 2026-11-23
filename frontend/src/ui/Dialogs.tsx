/**
 * Shared dialog shell.
 *
 * Every modal in the app (workflow browser, AI generate, settings, human input,
 * run inputs) needs the same three behaviours, and getting any of them wrong is
 * annoying in a way users notice:
 *
 *  - Escape closes.
 *  - A backdrop click closes, but a click *inside* does not (the classic
 *    `stopPropagation` mistake is avoided by testing the event target instead).
 *  - Body scroll is restored even if the dialog unmounts abruptly.
 */

import { useEffect, useRef, type ReactNode } from 'react';
import { X } from 'lucide-react';

import { useT } from '@/i18n';

/**
 * Which modal is on top of the stack.
 *
 * Dialogs can nest (opening a workflow while the current one has unsaved
 * changes asks a confirm *on top of* the browser). Each Dialog listens on
 * window for Escape, so without a stack one Escape would close both layers and
 * the underlying "discard?" question would be answered by accident.
 */
const dialogStack: symbol[] = [];

export function Dialog({
  title,
  onClose,
  children,
  footer,
  wide,
  icon,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
  icon?: ReactNode;
}) {
  const t = useT();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const token = useRef(Symbol('dialog'));

  // Move focus into the dialog when it opens and give it back when it closes.
  // Without this the element that opened the modal keeps focus, so a keyboard
  // user is still typing into the page *behind* the overlay: Escape is judged
  // "the user is editing a field" (the header name input, for example) and the
  // dialog refuses to close, while Tab walks the hidden background.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    dialogStack.push(token.current);
    panelRef.current?.focus();
    return () => {
      const index = dialogStack.indexOf(token.current);
      if (index >= 0) dialogStack.splice(index, 1);
      previous?.focus?.();
    };
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      // Only the topmost dialog may consume Escape.
      if (dialogStack[dialogStack.length - 1] !== token.current) return;
      // Escape inside a textarea should not close the whole dialog; the user is
      // probably abandoning an edit, not the modal.
      const target = event.target as HTMLElement | null;
      const editing =
        target instanceof HTMLTextAreaElement ||
        (target instanceof HTMLInputElement && target.type === 'text');
      if (event.key === 'Escape' && !editing) {
        event.stopPropagation();
        onClose();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose]);

  return (
    <div
      className="dialog-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        // Test the target rather than stopping propagation on the panel: the
        // latter breaks text selection that starts inside and ends outside.
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className={`dialog${wide ? ' dialog--wide' : ''}`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={panelRef}
        tabIndex={-1}
      >
        <header className="dialog__header">
          {icon}
          <span className="dialog__title">{title}</span>
          <div className="spacer" />
          <button
            type="button"
            className="btn btn--ghost btn--icon btn--sm"
            onClick={onClose}
            title={t('common.close')}
          >
            <X size={12} />
          </button>
        </header>

        <div className="dialog__body">{children}</div>

        {footer ? <footer className="dialog__footer">{footer}</footer> : null}
      </div>
    </div>
  );
}

export function ConfirmDialog({
  title,
  message,
  confirmLabel,
  danger,
  onConfirm,
  onCancel,
}: {
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const t = useT();

  return (
    <Dialog
      title={title}
      onClose={onCancel}
      footer={
        <>
          <div className="spacer" />
          <button type="button" className="btn" onClick={onCancel}>
            {t('common.cancel')}
          </button>
          <button
            type="button"
            className={`btn ${danger ? 'btn--danger' : 'btn--primary'}`}
            onClick={onConfirm}
          >
            {confirmLabel ?? t('common.confirm')}
          </button>
        </>
      }
    >
      <p>{message}</p>
    </Dialog>
  );
}
