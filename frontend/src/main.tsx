/**
 * Application entry point.
 *
 * Stylesheets are imported here rather than from components so their cascade
 * order is explicit: tokens define the variables every later file reads, and the
 * shell (`base`) must come before the panels that refine it.
 */

import { Component, StrictMode, type ErrorInfo, type ReactNode } from 'react';
import { createRoot } from 'react-dom/client';

import { GateRoot } from '@/access/GateRoot';

// Self-hosted fonts. Imported here rather than linked from a CDN because the
// app is used from mainland China, where Google Fonts is frequently blocked —
// a blocked stylesheet would silently leave the UI on a fallback face.
import '@fontsource-variable/inter';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';

import '@/styles/tokens.css';
import '@/styles/base.css';
import '@/styles/components.css';
import '@/styles/header.css';
import '@/styles/canvas.css';
import '@/styles/copilot.css';
import '@/styles/inspector.css';
// Ready-made Markdown rendering, scoped to `.markdown-body` and themed for dark
// surfaces. Both are MIT/BSD; nothing here is hand-rolled.
import 'github-markdown-css/github-markdown-dark.css';
import 'highlight.js/styles/github-dark.css';
import '@/styles/run.css';
import '@/styles/db.css';
import '@/styles/versions.css';
import '@/styles/gate.css';
// Last, deliberately: the visual refresh layer overrides the component styles
// above with the same selectors (tokens.css carries the values it reads).
import '@/styles/refresh.css';

/**
 * Last-resort boundary.
 *
 * A React render error blanks the whole document, which for a canvas app means
 * the user loses their graph with no explanation. This keeps the message on
 * screen and offers a reload.
 */
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Deliberately console-only: there is no telemetry endpoint, and swallowing
    // the stack would make a blank canvas impossible to diagnose.
    console.error('Unhandled render error', error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <div className="crash">
        <h1 className="crash__title">Something went wrong</h1>
        <p className="crash__message">{error.message}</p>
        <pre className="crash__stack scroll">{error.stack ?? 'No stack trace.'}</pre>
        <div className="row">
          <button type="button" className="btn btn--primary" onClick={() => window.location.reload()}>
            Reload
          </button>
          <button type="button" className="btn" onClick={() => this.setState({ error: null })}>
            Try to continue
          </button>
        </div>
      </div>
    );
  }
}

const container = document.getElementById('root');
if (!container) throw new Error('No #root element in index.html.');

// Apply the stored theme before the first paint. `tokens.css` defaults to light,
// so a dark preference has to be on <html> before React renders or the app
// flashes a white screen at someone who chose dark (and vice versa).
// `?theme=light|dark` wins for a single load (embeds / demos / screenshots).
try {
  const forced = new URLSearchParams(window.location.search).get('theme');
  const savedTheme = localStorage.getItem('scaffold.theme');
  document.documentElement.dataset.theme =
    forced === 'dark' || forced === 'light'
      ? forced
      : savedTheme === 'dark'
        ? 'dark'
        : 'light';
} catch {
  document.documentElement.dataset.theme = 'light';
}

createRoot(container).render(
  <StrictMode>
    <ErrorBoundary>
      <GateRoot />
    </ErrorBoundary>
  </StrictMode>,
);
