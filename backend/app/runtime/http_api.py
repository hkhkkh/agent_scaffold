"""A standard-library HTTP face for a deployed agent: chat, sessions, files.

Why ``http.server`` and not FastAPI
-----------------------------------
The exported agent is a *source copy* of the runtime (``app/runtime/export.py``),
and ``tests/test_layer_boundary.py`` fails if a runtime module so much as names
``fastapi``, ``starlette``, ``sqlmodel`` or ``app.api`` - at any nesting depth,
because the line travels even when it never runs here. An exported package also
installs only what its ``requirements.txt`` lists, and FastAPI is not in it. So
this module uses the standard library's threaded HTTP server and nothing else,
which is why a deployment with zero third-party dependencies can still serve a
chat.

What it exposes
---------------
* ``GET /healthz`` - liveness, never requires the token.
* ``GET /`` - a self-contained vanilla-JS chat page (no CDN, no build step).
* ``GET /api/info`` - what this deployment can do.
* ``POST /v1/invoke`` - one synchronous run, no conversation.
* ``/v1/sessions[...]`` - conversation CRUD and turns, optionally SSE-streamed.
* ``/v1/files[...]`` - upload/list/download/delete inside the workspace.

Token auth is optional: with ``HTTPConfig.token`` set, every ``/v1/*`` and
``/api/info`` request needs ``Authorization: Bearer <token>``. The health probe
and the chat page deliberately do not (the page asks for the token and keeps it
in ``localStorage``). Errors are always ``{"ok": false, "error": "..."}``, and a
traceback is logged to stderr, never sent to the client.

Threading model
---------------
``ThreadingHTTPServer`` gives each request its own thread; a request that needs
to await runs one ``asyncio.run`` in that thread. ``SessionService`` serialises
turns of one session with an :class:`asyncio.Lock`, which belongs to one loop, so
this module adds a per-session :class:`threading.Lock` around each session turn.
The two layers are documented where they are built.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import sys
import threading
import traceback
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlsplit

from app.runtime.session import SessionNotFound, SessionService
from app.runtime.workspace import Workspace, WorkspaceError, workspace_from_env

__all__ = ["AgentHTTPServer", "CHAT_PAGE", "HTTPConfig", "serve_http"]

#: CORS headers sent on every ``/v1/*`` response and preflight. The chat page is
#: served from the same origin, so these exist for a front end hosted elsewhere.
CORS_ALLOW_ORIGIN = "*"
CORS_ALLOW_METHODS = "GET, POST, DELETE, OPTIONS"
CORS_ALLOW_HEADERS = "Authorization, Content-Type"
CORS_MAX_AGE = "86400"


@dataclass
class HTTPConfig:
    """Where to listen, and how the deployment is presented.

    ``token`` is compared in constant time; an empty token disables auth, which
    is the right default for a process already bound to ``127.0.0.1``.
    ``max_body_bytes`` caps a whole JSON request (and therefore an upload, since
    the file travels base64-encoded inside it). ``chat_page`` lets a deployment
    expose the JSON API only.
    """

    host: str = "127.0.0.1"
    port: int = 8080
    token: str = ""
    max_body_bytes: int = 32 * 1024 * 1024
    agent_name: str = ""
    chat_page: bool = True


class _HTTPError(Exception):
    """A failure that becomes a JSON response instead of a traceback."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _dumps(payload: Any) -> bytes:
    """JSON bytes for a response body.

    ``default=str`` keeps an exotic run output (a datetime, a model object) out
    of the error path: the client wants the shape, not a 500 that says one leaf
    was not a primitive.
    """
    return json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")


def _content_disposition(name: str) -> str:
    """An attachment header that works for Chinese file names too.

    HTTP header values are latin-1 on the wire, so the readable ``filename=``
    gets an ASCII fallback and the real name travels in RFC 5987 ``filename*=``.
    """
    base = name.rsplit("/", 1)[-1]
    ascii_name = base.encode("ascii", "replace").decode("ascii").replace('"', "_")
    return (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(base, safe='')}"
    )

#: The built-in chat page. One self-contained document: inline CSS, inline JS,
#: no external CDN, no font download, no build step. It talks only to the routes
#: above, so it works against any deployment, including one on a laptop with no
#: network. Deliberately plain ``var``/``function`` JavaScript, so it keeps
#: working in the browsers old client machines still have.
CHAT_PAGE = r"""<!DOCTYPE html><!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent 对话</title>
<style>
  :root {
    color-scheme: light;
    --bg: #f4f5f7; --bg-elev: #ffffff; --bg-soft: #f2f3f5; --bg-sunken: #eceef1;
    --line: #e4e6eb; --line-soft: #eef0f3;
    --text: #1d2129; --text-2: #4e5969; --text-3: #86909c;
    --accent: #4d53e8; --accent-hover: #3f45d6; --accent-soft: #ecedfd; --accent-line: rgba(77, 83, 232, 0.22);
    --grad: linear-gradient(135deg, #6366f1 0%, #8b5cf6 55%, #d946ef 100%);
    --success: #00b42a; --warning: #ff7d00; --danger: #f53f3f;
    --shadow-sm: 0 1px 2px rgba(29, 33, 41, 0.05), 0 1px 3px rgba(29, 33, 41, 0.04);
    --shadow-md: 0 8px 28px rgba(29, 33, 41, 0.08);
    --radius-sm: 8px; --radius: 12px; --radius-lg: 16px; --radius-pill: 999px;
    --rail: 296px; --thread: 860px; --transition: 140ms ease;
    --font: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
    --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "Liberation Mono", monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --bg: #17181c; --bg-elev: #1e2026; --bg-soft: #24262d; --bg-sunken: #131418;
      --line: #2c2f37; --line-soft: #262931;
      --text: #e8eaf0; --text-2: #a9aeb8; --text-3: #7a808c;
      --accent: #7b80ff; --accent-hover: #9aa0ff; --accent-soft: rgba(123, 128, 255, 0.14); --accent-line: rgba(123, 128, 255, 0.3);
      --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4); --shadow-md: 0 8px 28px rgba(0, 0, 0, 0.45);
    }
  }
  html[data-theme="light"] {
    color-scheme: light;
    --bg: #f4f5f7; --bg-elev: #ffffff; --bg-soft: #f2f3f5; --bg-sunken: #eceef1;
    --line: #e4e6eb; --line-soft: #eef0f3; --text: #1d2129; --text-2: #4e5969; --text-3: #86909c;
    --accent: #4d53e8; --accent-hover: #3f45d6; --accent-soft: #ecedfd; --accent-line: rgba(77, 83, 232, 0.22);
    --shadow-sm: 0 1px 2px rgba(29, 33, 41, 0.05), 0 1px 3px rgba(29, 33, 41, 0.04);
    --shadow-md: 0 8px 28px rgba(29, 33, 41, 0.08);
  }
  html[data-theme="dark"] {
    color-scheme: dark;
    --bg: #17181c; --bg-elev: #1e2026; --bg-soft: #24262d; --bg-sunken: #131418;
    --line: #2c2f37; --line-soft: #262931; --text: #e8eaf0; --text-2: #a9aeb8; --text-3: #7a808c;
    --accent: #7b80ff; --accent-hover: #9aa0ff; --accent-soft: rgba(123, 128, 255, 0.14); --accent-line: rgba(123, 128, 255, 0.3);
    --shadow-sm: 0 1px 2px rgba(0, 0, 0, 0.4); --shadow-md: 0 8px 28px rgba(0, 0, 0, 0.45);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; background: var(--bg); color: var(--text); font-family: var(--font); font-size: 14px; line-height: 1.6; -webkit-font-smoothing: antialiased; }
  button, input, textarea { font: inherit; color: inherit; }
  button { cursor: pointer; }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 6px; }
  .icon { width: 18px; height: 18px; flex: none; display: block; }
  .icon-sm { width: 15px; height: 15px; flex: none; display: block; }
  svg.icon, svg.icon-sm { fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
  .app { display: flex; height: 100%; overflow: hidden; }

  .rail { width: var(--rail); flex: none; background: var(--bg-elev); border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; z-index: 30; }
  .brand { display: flex; align-items: center; gap: 12px; padding: 18px 18px 14px; }
  .avatar { width: 40px; height: 40px; border-radius: 12px; background: var(--grad); color: #fff; display: flex; align-items: center; justify-content: center; font-size: 18px; font-weight: 600; flex: none; box-shadow: 0 4px 12px rgba(99, 102, 241, 0.35); letter-spacing: 0.5px; }
  .brand-text { min-width: 0; }
  .brand-name { font-size: 14px; font-weight: 600; line-height: 1.35; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .brand-sub { font-size: 12px; color: var(--text-3); display: flex; align-items: center; gap: 5px; }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--success); display: inline-block; }
  .rail-actions { padding: 0 16px 12px; }
  .btn-primary { width: 100%; display: flex; align-items: center; justify-content: center; gap: 7px; padding: 9px 14px; border: none; border-radius: var(--radius-sm); background: var(--accent); color: #fff; font-weight: 500; transition: background var(--transition), transform var(--transition); box-shadow: 0 4px 14px rgba(77, 83, 232, 0.28); }
  .btn-primary:hover { background: var(--accent-hover); }
  .btn-primary:active { transform: translateY(1px); }
  .rail-section { display: flex; flex-direction: column; min-height: 0; }
  .rail-section + .rail-section { border-top: 1px solid var(--line-soft); margin-top: 4px; }
  #sessionSection { flex: 1 1 auto; }
  #fileSection { flex: 0 1 42%; }
  .rail-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 12px 18px 6px; font-size: 12px; font-weight: 600; color: var(--text-3); letter-spacing: 0.4px; }
  .rail-head .count { font-weight: 400; }
  .scroll-list { overflow-y: auto; padding: 2px 10px 10px; min-height: 0; }
  .session-item { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: var(--radius-sm); border: 1px solid transparent; transition: background var(--transition), border-color var(--transition); }
  .session-item:hover { background: var(--bg-soft); }
  .session-item.active { background: var(--accent-soft); border-color: var(--accent-line); }
  .session-title { flex: 1; min-width: 0; background: none; border: none; padding: 0; text-align: left; font-size: 13px; color: var(--text); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .session-item.active .session-title { color: var(--accent); font-weight: 500; }
  .session-meta { font-size: 11px; color: var(--text-3); flex: none; }
  .session-del { width: 22px; height: 22px; border: none; border-radius: 6px; background: transparent; color: var(--text-3); display: none; align-items: center; justify-content: center; flex: none; padding: 0; }
  .session-item:hover .session-del { display: flex; }
  .session-del:hover { background: var(--bg-sunken); color: var(--danger); }
  .file-item { display: flex; align-items: center; gap: 9px; padding: 7px 10px; border-radius: var(--radius-sm); transition: background var(--transition); cursor: pointer; border: none; background: none; width: 100%; text-align: left; }
  .file-item:hover { background: var(--bg-soft); }
  .file-badge { width: 30px; height: 30px; border-radius: 8px; flex: none; display: flex; align-items: center; justify-content: center; font-size: 9px; font-weight: 700; letter-spacing: 0.2px; background: var(--bg-soft); color: var(--text-2); }
  .file-badge.xlsx { background: #e8f7ee; color: #0a8f4b; }
  .file-badge.csv { background: #e6f4ff; color: #1a73e8; }
  .file-badge.pdf { background: #fdeaea; color: #d93026; }
  .file-badge.docx { background: #e8efff; color: #2f54eb; }
  html[data-theme="dark"] .file-badge.xlsx { background: rgba(0, 180, 42, 0.14); color: #4ade80; }
  html[data-theme="dark"] .file-badge.csv { background: rgba(56, 149, 255, 0.16); color: #7cb7ff; }
  html[data-theme="dark"] .file-badge.pdf { background: rgba(245, 63, 63, 0.16); color: #ff8a80; }
  html[data-theme="dark"] .file-badge.docx { background: rgba(47, 84, 235, 0.2); color: #8fa6ff; }
  .file-text { min-width: 0; flex: 1; display: flex; flex-direction: column; }
  .file-name { font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .file-size { font-size: 11px; color: var(--text-3); }
  .rail-foot { padding: 10px 18px 14px; border-top: 1px solid var(--line-soft); font-size: 11px; color: var(--text-3); display: flex; align-items: center; gap: 6px; }
  .empty-mini { padding: 10px 12px; font-size: 12px; color: var(--text-3); line-height: 1.55; }

  .main { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .topbar { height: 56px; flex: none; display: flex; align-items: center; gap: 10px; padding: 0 18px; border-bottom: 1px solid var(--line); background: var(--bg-elev); }
  .topbar .title { font-size: 14px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0; flex: 0 1 auto; }
  .topbar .spacer { flex: 1; }
  .icon-btn { width: 34px; height: 34px; border-radius: var(--radius-sm); border: 1px solid transparent; background: transparent; color: var(--text-2); display: inline-flex; align-items: center; justify-content: center; transition: background var(--transition), color var(--transition); padding: 0; }
  .icon-btn:hover { background: var(--bg-soft); color: var(--text); }
  .only-mobile { display: none; }

  .thread { flex: 1; overflow-y: auto; overflow-x: hidden; min-height: 0; }
  .thread-inner { max-width: var(--thread); margin: 0 auto; padding: 28px 24px 12px; display: flex; flex-direction: column; gap: 22px; }

  .msg { display: flex; gap: 12px; animation: rise 180ms ease both; }
  .msg.user { justify-content: flex-end; }
  .msg-avatar { width: 30px; height: 30px; border-radius: 9px; flex: none; display: flex; align-items: center; justify-content: center; font-size: 13px; font-weight: 600; color: #fff; background: var(--grad); }
  .msg-body { min-width: 0; max-width: 82%; }
  .msg-head { display: flex; align-items: baseline; gap: 8px; margin: 0 0 6px 2px; }
  .msg-name { font-size: 13px; font-weight: 600; color: var(--text-2); }
  .msg-time { font-size: 11px; color: var(--text-3); }
  .bubble { padding: 12px 15px; border-radius: 4px 14px 14px 14px; background: var(--bg-elev); border: 1px solid var(--line); box-shadow: var(--shadow-sm); word-break: break-word; overflow-wrap: anywhere; max-width: 100%; font-size: 14px; line-height: 1.72; color: var(--text); }
  .msg.user .bubble { background: var(--accent-soft); border-color: var(--accent-line); border-radius: 14px 4px 14px 14px; box-shadow: none; white-space: pre-wrap; }
  .msg-actions { margin: 6px 2px 0; display: flex; gap: 6px; opacity: 0; transition: opacity var(--transition); }
  .msg:hover .msg-actions { opacity: 1; }
  .chip-btn { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px; font-size: 11px; color: var(--text-3); background: transparent; border: 1px solid var(--line); border-radius: var(--radius-pill); }
  .chip-btn:hover { color: var(--text); border-color: var(--text-3); }
  .attach-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
  .attach { display: inline-flex; align-items: center; gap: 8px; padding: 6px 10px; border-radius: var(--radius-sm); background: var(--bg-elev); border: 1px solid var(--line); font-size: 12px; color: var(--text-2); }
  .msg.user .attach { background: rgba(255, 255, 255, 0.65); }
  html[data-theme="dark"] .msg.user .attach { background: rgba(255, 255, 255, 0.06); }

  .bubble > :first-child { margin-top: 0; }
  .bubble > :last-child { margin-bottom: 0; }
  .bubble p { margin: 0 0 10px; }
  .bubble h1, .bubble h2, .bubble h3, .bubble h4 { margin: 16px 0 8px; line-height: 1.4; font-weight: 600; }
  .bubble h1 { font-size: 19px; }
  .bubble h2 { font-size: 17px; }
  .bubble h3 { font-size: 15px; }
  .bubble h4 { font-size: 14px; }
  .bubble ul, .bubble ol { margin: 0 0 10px; padding-left: 22px; }
  .bubble li { margin: 3px 0; }
  .bubble code { font-family: var(--mono); font-size: 12.5px; background: var(--bg-soft); border: 1px solid var(--line-soft); border-radius: 5px; padding: 1px 5px; }
  .bubble pre { background: var(--bg-sunken); border: 1px solid var(--line); border-radius: var(--radius-sm); padding: 12px 14px; overflow-x: auto; margin: 0 0 10px; }
  .bubble pre code { background: none; border: none; padding: 0; font-size: 12.5px; line-height: 1.6; }
  .bubble blockquote { margin: 0 0 10px; padding: 4px 0 4px 12px; border-left: 3px solid var(--accent-line); color: var(--text-2); }
  .bubble hr { border: none; border-top: 1px solid var(--line); margin: 14px 0; }
  .bubble table { border-collapse: collapse; width: 100%; max-width: 100%; margin: 0 0 12px; font-size: 13px; display: block; overflow-x: auto; }
  .bubble th, .bubble td { border: 1px solid var(--line); padding: 7px 10px; text-align: left; white-space: nowrap; }
  .bubble th { background: var(--bg-soft); font-weight: 600; }
  .bubble tr:nth-child(even) td { background: var(--bg-soft); }
  .bubble a { color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent-line); }

  .empty-state { max-width: 560px; margin: 13vh auto 0; text-align: center; padding: 0 16px; }
  .orb { width: 64px; height: 64px; margin: 0 auto 18px; border-radius: 20px; background: var(--grad); display: flex; align-items: center; justify-content: center; color: #fff; box-shadow: 0 12px 32px rgba(99, 102, 241, 0.35); }
  .orb .icon { width: 30px; height: 30px; }
  .empty-state h2 { margin: 0 0 8px; font-size: 21px; font-weight: 600; letter-spacing: 0.2px; }
  .empty-state p { margin: 0 auto 22px; color: var(--text-2); font-size: 14px; max-width: 440px; }
  .suggestions { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
  .suggestion { display: inline-flex; align-items: center; gap: 8px; padding: 10px 14px; border-radius: var(--radius); background: var(--bg-elev); border: 1px solid var(--line); box-shadow: var(--shadow-sm); font-size: 13px; color: var(--text-2); transition: border-color var(--transition), color var(--transition), transform var(--transition); }
  .suggestion:hover { border-color: var(--accent-line); color: var(--accent); transform: translateY(-1px); }

  .typing { display: inline-flex; align-items: center; gap: 8px; color: var(--text-3); font-size: 13px; }
  .dots { display: inline-flex; gap: 4px; }
  .dots i { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); opacity: 0.35; animation: blink 1.2s infinite; }
  .dots i:nth-child(2) { animation-delay: 0.2s; }
  .dots i:nth-child(3) { animation-delay: 0.4s; }

  .composer-wrap { flex: none; padding: 8px 24px 18px; background: var(--bg); }
  .composer-inner { max-width: var(--thread); margin: 0 auto; }
  .pending-row { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
  .pending-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 6px 4px 10px; font-size: 12px; border-radius: var(--radius-pill); background: var(--accent-soft); color: var(--accent); border: 1px solid var(--accent-line); }
  .pending-chip button { border: none; background: none; color: var(--accent); padding: 0 4px; font-size: 15px; line-height: 1; }
  .composer { border: 1px solid var(--line); background: var(--bg-elev); border-radius: var(--radius-lg); box-shadow: var(--shadow-md); padding: 8px 10px 8px 14px; transition: border-color var(--transition), box-shadow var(--transition); }
  .composer:focus-within { border-color: var(--accent-line); box-shadow: 0 0 0 3px var(--accent-soft), var(--shadow-md); }
  .composer textarea { width: 100%; border: none; outline: none; resize: none; background: transparent; color: var(--text); font-size: 14px; line-height: 1.6; padding: 6px 0 8px; max-height: 180px; min-height: 28px; }
  .composer textarea::placeholder { color: var(--text-3); }
  .composer-row { display: flex; align-items: center; gap: 8px; }
  .composer-row .spacer { flex: 1; }
  .attach-btn { width: 32px; height: 32px; border-radius: var(--radius-sm); border: 1px solid transparent; background: transparent; color: var(--text-2); display: inline-flex; align-items: center; justify-content: center; padding: 0; }
  .attach-btn:hover { background: var(--bg-soft); color: var(--text); }
  .hint { font-size: 11.5px; color: var(--text-3); padding-right: 2px; }
  .send-btn { width: 34px; height: 34px; border: none; border-radius: 10px; background: var(--accent); color: #fff; display: inline-flex; align-items: center; justify-content: center; transition: background var(--transition), transform var(--transition), opacity var(--transition); box-shadow: 0 4px 12px rgba(77, 83, 232, 0.3); padding: 0; }
  .send-btn:hover { background: var(--accent-hover); }
  .send-btn:active { transform: translateY(1px); }
  .send-btn:disabled { opacity: 0.4; cursor: not-allowed; box-shadow: none; }
  .stop-btn { background: var(--bg-soft); color: var(--text-2); box-shadow: none; border: 1px solid var(--line); }

  .modal-mask { position: fixed; inset: 0; background: rgba(15, 17, 22, 0.45); display: none; align-items: center; justify-content: center; padding: 24px; z-index: 60; }
  .modal-mask.open { display: flex; }
  .modal { width: 100%; max-width: 440px; background: var(--bg-elev); border-radius: var(--radius-lg); border: 1px solid var(--line); box-shadow: var(--shadow-md); padding: 22px; animation: rise 160ms ease both; }
  .modal h3 { margin: 0 0 4px; font-size: 16px; }
  .modal p { margin: 0 0 16px; font-size: 12.5px; color: var(--text-3); }
  .modal input { width: 100%; padding: 10px 12px; border-radius: var(--radius-sm); border: 1px solid var(--line); background: var(--bg-soft); color: var(--text); outline: none; font-size: 13px; }
  .modal input:focus { border-color: var(--accent-line); box-shadow: 0 0 0 3px var(--accent-soft); }
  .modal-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 18px; }
  .btn-ghost { padding: 8px 14px; border-radius: var(--radius-sm); border: 1px solid var(--line); background: var(--bg-elev); color: var(--text-2); font-size: 13px; }
  .btn-ghost:hover { background: var(--bg-soft); }

  .toast { position: fixed; top: 18px; left: 50%; transform: translate(-50%, -12px); z-index: 80; padding: 10px 16px; border-radius: var(--radius-sm); background: var(--bg-elev); border: 1px solid var(--line); box-shadow: var(--shadow-md); font-size: 13px; opacity: 0; pointer-events: none; transition: opacity var(--transition), transform var(--transition); max-width: min(480px, 90vw); }
  .toast.show { opacity: 1; transform: translate(-50%, 0); }
  .toast.error { border-color: var(--danger); color: var(--danger); }
  .drop-overlay { position: fixed; inset: 12px; z-index: 70; display: none; align-items: center; justify-content: center; background: rgba(77, 83, 232, 0.08); border: 2px dashed var(--accent); border-radius: var(--radius-lg); color: var(--accent); font-size: 15px; font-weight: 500; pointer-events: none; }
  .drop-overlay.show { display: flex; }

  @keyframes blink { 0%, 80%, 100% { opacity: 0.25; transform: translateY(0); } 40% { opacity: 1; transform: translateY(-2px); } }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }

  @media (max-width: 900px) {
    .rail { position: fixed; inset: 0 auto 0 0; transform: translateX(-100%); transition: transform var(--transition); }
    body.rail-open .rail { transform: translateX(0); box-shadow: var(--shadow-md); }
    body.rail-open::after { content: ""; position: fixed; inset: 0; background: rgba(15, 17, 22, 0.4); z-index: 20; }
    .only-mobile { display: inline-flex; }
    .thread-inner { padding: 20px 14px 8px; }
    .composer-wrap { padding: 6px 12px 14px; }
    .msg-body { max-width: 88%; }
    .msg.user .msg-body { max-width: 86%; }
    .topbar { padding: 0 12px; }
    .empty-state { margin-top: 5vh; }
    .hint { display: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    * { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  }
</style>
</head>
<body>
<div class="app">
  <aside class="rail" id="rail" aria-label="侧栏">
    <div class="brand">
      <div class="avatar" id="brandAvatar">AI</div>
      <div class="brand-text">
        <div class="brand-name" id="brandName">智能体</div>
        <div class="brand-sub"><span class="dot"></span><span>在线 · 独立部署</span></div>
      </div>
    </div>
    <div class="rail-actions">
      <button class="btn-primary" id="newSession" type="button">
        <svg class="icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>
        新建会话
      </button>
    </div>
    <div class="rail-section" id="sessionSection">
      <div class="rail-head"><span>会话</span><span class="count" id="sessionCount">0</span></div>
      <div class="scroll-list" id="sessionList" role="list"></div>
    </div>
    <div class="rail-section" id="fileSection">
      <div class="rail-head"><span>工作区文件</span></div>
      <div class="scroll-list" id="fileList" role="list"></div>
    </div>
    <div class="rail-foot">
      <svg class="icon-sm" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 4 7v6c0 4.4 3.4 7.4 8 8 4.6-.6 8-3.6 8-8V7l-8-4Z"/><path d="m9 12 2 2 4-4"/></svg>
      <span>会话与文件保存在本机</span>
    </div>
  </aside>

  <main class="main">
    <header class="topbar">
      <button class="icon-btn only-mobile" id="railToggle" type="button" aria-label="打开侧栏">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
      </button>
      <div class="title" id="topTitle">智能体对话</div>
      <div class="spacer"></div>
      <button class="icon-btn" id="refresh" type="button" title="刷新">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M21 12a9 9 0 1 1-2.6-6.4M21 3v6h-6"/></svg>
      </button>
      <button class="icon-btn" id="themeToggle" type="button" title="切换主题">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
      </button>
      <button class="icon-btn" id="settingsBtn" type="button" title="访问令牌">
        <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.9 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.9.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.9-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.9V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1Z"/></svg>
      </button>
    </header>

    <section class="thread" id="thread" aria-live="polite">
      <div class="thread-inner" id="threadInner"></div>
    </section>

    <div class="composer-wrap">
      <div class="composer-inner">
        <div class="pending-row" id="pending"></div>
        <div class="composer">
          <textarea id="input" rows="1" placeholder="输入消息，Enter 发送，Shift + Enter 换行" aria-label="消息输入"></textarea>
          <div class="composer-row">
            <button class="attach-btn" id="attachBtn" type="button" title="上传文件">
              <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="m21.4 11.05-9.2 9.2a6 6 0 0 1-8.49-8.49l9.2-9.19a4 4 0 0 1 5.65 5.66l-9.2 9.19a2 2 0 0 1-2.82-2.83l8.49-8.48"/></svg>
            </button>
            <div class="spacer"></div>
            <span class="hint" id="hint">Enter 发送 · Shift+Enter 换行 · 可拖拽文件上传</span>
            <button class="send-btn" id="send" type="button" title="发送" aria-label="发送">
              <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5m0 0-6 6m6-6 6 6"/></svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  </main>
</div>

<div class="modal-mask" id="tokenModal" role="dialog" aria-modal="true" aria-labelledby="tokenTitle">
  <div class="modal">
    <h3 id="tokenTitle">访问令牌</h3>
    <p>该部署开启鉴权时填写；令牌只保存在此浏览器本地。</p>
    <input id="tokenInput" type="password" autocomplete="off" placeholder="Bearer token">
    <div class="modal-actions">
      <button class="btn-ghost" id="tokenClear" type="button">清除</button>
      <button class="btn-primary" id="tokenSave" type="button" style="width:auto;padding:8px 18px">保存</button>
    </div>
  </div>
</div>

<div class="drop-overlay" id="dropOverlay">释放以上传文件到工作区</div>
<div class="toast" id="toast" role="status"></div>
<input id="file" type="file" multiple hidden>

<script>
(function () {
  'use strict';

  var THEME_KEY = 'agent.theme';
  var TOKEN_KEY = 'agent.token';

  var state = {
    sessions: [], currentId: null, messages: [], files: [], pending: [],
    token: '', theme: 'light', busy: false, typingNode: null,
    timer: null, startedAt: 0, controller: null
  };

  function byId(id) { return document.getElementById(id); }
  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text != null) { node.textContent = text; }
    return node;
  }
  function svgIcon(paths, cls) {
    var parts = paths.split('|');
    var inner = '';
    for (var i = 0; i < parts.length; i++) { inner += '<path d="' + parts[i] + '"/>'; }
    return '<svg class="' + (cls || 'icon-sm') + '" viewBox="0 0 24 24" aria-hidden="true">' + inner + '</svg>';
  }

  function toast(text, isError) {
    var node = byId('toast');
    node.textContent = text || '';
    node.className = 'toast show' + (isError ? ' error' : '');
    window.clearTimeout(toast._timer);
    toast._timer = window.setTimeout(function () { node.className = 'toast'; }, 4200);
  }

  function timeAgo(iso) {
    if (!iso) { return ''; }
    var then = new Date(iso).getTime();
    if (isNaN(then)) { return ''; }
    var diff = Math.max(0, Date.now() - then) / 1000;
    if (diff < 60) { return '刚刚'; }
    if (diff < 3600) { return Math.floor(diff / 60) + ' 分钟前'; }
    if (diff < 86400) { return Math.floor(diff / 3600) + ' 小时前'; }
    return Math.floor(diff / 86400) + ' 天前';
  }

  function formatSize(size) {
    if (typeof size !== 'number') { return ''; }
    if (size < 1024) { return size + ' B'; }
    if (size < 1024 * 1024) { return (size / 1024).toFixed(1) + ' KB'; }
    return (size / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function extOf(name) {
    var parts = String(name || '').split('.');
    return parts.length > 1 ? parts.pop().toLowerCase() : 'file';
  }

  function applyTheme() {
    var root = document.documentElement;
    if (state.theme === 'auto') { root.removeAttribute('data-theme'); }
    else { root.setAttribute('data-theme', state.theme); }
  }

  function cycleTheme() {
    state.theme = state.theme === 'auto' ? 'light' : (state.theme === 'light' ? 'dark' : 'auto');
    try { localStorage.setItem(THEME_KEY, state.theme); } catch (error) {}
    applyTheme();
  }

  function headers() {
    var out = { 'Content-Type': 'application/json' };
    if (state.token) { out.Authorization = 'Bearer ' + state.token; }
    return out;
  }

  function api(path, options) {
    options = options || {};
    return fetch(path, {
      method: options.method || 'GET',
      headers: headers(),
      body: options.body === undefined ? undefined : JSON.stringify(options.body)
    }).then(function (response) {
      return response.text().then(function (raw) {
        var data = {};
        try { data = raw ? JSON.parse(raw) : {}; } catch (error) { data = {}; }
        if (!response.ok) {
          var failure = new Error(data.error || data.detail || ('HTTP ' + response.status));
          failure.status = response.status;
          throw failure;
        }
        return data;
      });
    });
  }

  // ---------------- rendering ----------------
  function renderSessions() {
    var list = byId('sessionList');
    list.innerHTML = '';
    byId('sessionCount').textContent = String(state.sessions.length);
    state.sessions.forEach(function (session) {
      var row = el('div', 'session-item' + (session.id === state.currentId ? ' active' : ''));
      row.setAttribute('role', 'listitem');
      var button = el('button', 'session-title', session.title || '未命名会话');
      button.type = 'button';
      button.title = session.title || session.id;
      button.onclick = function () { selectSession(session.id); };
      var meta = el('span', 'session-meta', timeAgo(session.updated_at));
      var remove = el('button', 'session-del');
      remove.type = 'button';
      remove.title = '删除会话';
      remove.innerHTML = svgIcon('M4 7h16|M9 7V5h6v2|M6 7l1 13h10l1-13|M10 11v5M14 11v5');
      remove.onclick = function (event) {
        event.stopPropagation();
        removeSession(session.id);
      };
      row.appendChild(button);
      row.appendChild(meta);
      row.appendChild(remove);
      list.appendChild(row);
    });
    if (!state.sessions.length) {
      list.appendChild(el('div', 'empty-mini', '还没有会话。发一条消息，或点「新建会话」开始。'));
    }
  }

  function renderFiles() {
    var list = byId('fileList');
    list.innerHTML = '';
    state.files.forEach(function (file) {
      var row = el('button', 'file-item');
      row.type = 'button';
      row.title = '下载 ' + file.name;
      var ext = extOf(file.name);
      var badge = el('span', 'file-badge ' + ext, ext === 'file' ? 'FILE' : ext.toUpperCase().slice(0, 4));
      var text = el('span', 'file-text');
      text.appendChild(el('span', 'file-name', file.name));
      text.appendChild(el('span', 'file-size', formatSize(file.size)));
      row.appendChild(badge);
      row.appendChild(text);
      row.insertAdjacentHTML('beforeend', svgIcon('M12 3v12m0 0-4-4m4 4 4-4|M4 17v3h16v-3'));
      row.onclick = function () { downloadFile(file.name); };
      list.appendChild(row);
    });
    if (!state.files.length) {
      list.appendChild(el('div', 'empty-mini', '工作区还没有文件。点回形针，或把文件拖进对话区。'));
    }
  }

  function renderPending() {
    var box = byId('pending');
    box.innerHTML = '';
    state.pending.forEach(function (item, index) {
      var chip = el('span', 'pending-chip');
      chip.appendChild(el('span', '', item.name));
      var remove = el('button', '', '×');
      remove.type = 'button';
      remove.title = '移除';
      remove.onclick = function () {
        state.pending.splice(index, 1);
        renderPending();
      };
      chip.appendChild(remove);
      box.appendChild(chip);
    });
  }

  function agentInitial() {
    var name = byId('brandName').textContent || 'AI';
    var first = name.trim().charAt(0);
    return first ? first.toUpperCase() : 'AI';
  }

  function makeAvatar() { return el('div', 'msg-avatar', agentInitial()); }

  function appendAttachments(container, attachments) {
    if (!attachments || !attachments.length) { return; }
    var row = el('div', 'attach-row');
    attachments.forEach(function (attachment) {
      var name = attachment.name || attachment.file || '附件';
      var chip = el('span', 'attach');
      chip.insertAdjacentHTML('beforeend', svgIcon('M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z|M14 2v6h6'));
      chip.appendChild(el('span', '', name));
      row.appendChild(chip);
    });
    container.appendChild(row);
  }

  function copyText(text, done) {
    function fallback() {
      var area = document.createElement('textarea');
      area.value = text;
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (error) { ok = false; }
      document.body.removeChild(area);
      done(ok);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
    } else { fallback(); }
  }

  function messageNode(message) {
    if (message.role === 'user') {
      var userRow = el('div', 'msg user');
      var body = el('div', 'msg-body');
      var bubble = el('div', 'bubble');
      bubble.textContent = message.content || '';
      appendAttachments(bubble, message.attachments);
      body.appendChild(bubble);
      userRow.appendChild(body);
      return userRow;
    }
    var row = el('div', 'msg assistant');
    row.appendChild(makeAvatar());
    var content = el('div', 'msg-body');
    var head = el('div', 'msg-head');
    head.appendChild(el('span', 'msg-name', byId('brandName').textContent || 'AI'));
    head.appendChild(el('span', 'msg-time', timeAgo(message.created_at)));
    content.appendChild(head);
    var bubble = el('div', 'bubble');
    bubble.innerHTML = renderMarkdown(message.content || '');
    appendAttachments(bubble, message.attachments);
    content.appendChild(bubble);
    var actions = el('div', 'msg-actions');
    var copy = el('button', 'chip-btn');
    copy.type = 'button';
    copy.innerHTML = svgIcon('M9 9h11v11H9z|M5 15H4V4h11v1');
    var copyLabel = el('span', '', '复制');
    copy.appendChild(copyLabel);
    copy.onclick = function () {
      copyText(message.content || '', function (ok) {
        copyLabel.textContent = ok ? '已复制' : '复制失败';
        window.setTimeout(function () { copyLabel.textContent = '复制'; }, 1600);
      });
    };
    actions.appendChild(copy);
    content.appendChild(actions);
    row.appendChild(content);
    return row;
  }

  function emptyState() {
    var wrap = el('div', 'empty-state');
    var orb = el('div', 'orb');
    orb.innerHTML = svgIcon('M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z|M18 15l.9 2.1L21 18l-2.1.9L18 21l-.9-2.1L15 18l2.1-.9z', 'icon');
    wrap.appendChild(orb);
    wrap.appendChild(el('h2', '', '你好，我是 ' + (byId('brandName').textContent || '智能体')));
    wrap.appendChild(el('p', '', '我能读取工作区里的表格、CSV、PDF 和 Word 文档，也能把整理好的结果写回文件。直接用自然语言提问就行。'));
    var suggestions = el('div', 'suggestions');
    var prompts = ['工作区里有哪些文件？', '帮我总结表格里的关键数字', '把结果整理成 CSV'];
    if (state.files.length) { prompts[0] = '看看 ' + state.files[0].name + ' 里有哪些数据？'; }
    prompts.forEach(function (text) {
      var chip = el('button', 'suggestion');
      chip.type = 'button';
      chip.innerHTML = svgIcon('M12 3v18M3 12h18');
      chip.appendChild(el('span', '', text));
      chip.onclick = function () {
        byId('input').value = text;
        autoGrow();
        byId('input').focus();
      };
      suggestions.appendChild(chip);
    });
    wrap.appendChild(suggestions);
    return wrap;
  }

  function scrollToBottom() {
    var thread = byId('thread');
    thread.scrollTop = thread.scrollHeight;
  }

  function renderMessages() {
    var inner = byId('threadInner');
    inner.innerHTML = '';
    state.typingNode = null;
    if (!state.messages.length) {
      inner.appendChild(emptyState());
      return;
    }
    state.messages.forEach(function (message) {
      inner.appendChild(messageNode(message));
    });
    scrollToBottom();
  }

  function showTyping() {
    var inner = byId('threadInner');
    var row = el('div', 'msg assistant');
    row.appendChild(makeAvatar());
    var content = el('div', 'msg-body');
    var head = el('div', 'msg-head');
    head.appendChild(el('span', 'msg-name', byId('brandName').textContent || 'AI'));
    content.appendChild(head);
    var bubble = el('div', 'bubble');
    var typing = el('span', 'typing');
    var dots = el('span', 'dots');
    dots.appendChild(el('i'));
    dots.appendChild(el('i'));
    dots.appendChild(el('i'));
    typing.appendChild(dots);
    var label = el('span', '', '正在思考…');
    typing.appendChild(label);
    bubble.appendChild(typing);
    content.appendChild(bubble);
    row.appendChild(content);
    inner.appendChild(row);
    state.typingNode = row;
    state.startedAt = Date.now();
    window.clearInterval(state.timer);
    state.timer = window.setInterval(function () {
      if (state.typingNode) {
        label.textContent = '正在思考… ' + Math.max(1, Math.floor((Date.now() - state.startedAt) / 1000)) + 's';
      }
    }, 1000);
    scrollToBottom();
  }

  function hideTyping() {
    window.clearInterval(state.timer);
    state.timer = null;
    state.typingNode = null;
  }

  // ---------------- markdown ----------------
  function inline(text) {
    var codes = [];
    text = text.replace(/`([^`]+)`/g, function (_, code) {
      codes.push(code);
      return '\u0001' + (codes.length - 1) + '\u0001';
    });
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, function (_, before, body) {
      return before + '<em>' + body + '</em>';
    });
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (match, label, href) {
      var clean = href.replace(/&amp;/g, '&');
      if (!/^(https?:|mailto:|tel:)/i.test(clean)) { return match; }
      return '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });
    text = text.replace(/\u0001(\d+)\u0001/g, function (_, index) {
      return '<code>' + codes[Number(index)] + '</code>';
    });
    return text;
  }

  function isTableSeparator(line) {
    return /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(line);
  }

  function splitRow(line) {
    var trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    var cells = [];
    var current = '';
    var inCode = false;
    for (var i = 0; i < trimmed.length; i++) {
      var ch = trimmed.charAt(i);
      if (ch === '`') { inCode = !inCode; current += ch; continue; }
      if (ch === '|' && !inCode) { cells.push(current.trim()); current = ''; continue; }
      current += ch;
    }
    cells.push(current.trim());
    return cells;
  }

  function renderMarkdown(raw) {
    var text = String(raw || '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
    var lines = text.split('\n');
    var out = [];
    var i = 0;
    var paragraph = [];
    function flush() {
      if (!paragraph.length) { return; }
      out.push('<p>' + paragraph.map(inline).join('<br>') + '</p>');
      paragraph = [];
    }
    while (i < lines.length) {
      var line = lines[i];
      if (/^```/.test(line)) {
        flush();
        var lang = line.replace(/^```/, '').trim();
        var code = [];
        i += 1;
        while (i < lines.length && !/^```/.test(lines[i])) { code.push(lines[i]); i += 1; }
        i += 1;
        out.push('<pre><code' + (lang ? ' class="lang-' + esc(lang) + '"' : '') + '>' + esc(code.join('\n')) + '</code></pre>');
        continue;
      }
      var heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        flush();
        var level = heading[1].length;
        out.push('<h' + level + '>' + inline(esc(heading[2])) + '</h' + level + '>');
        i += 1;
        continue;
      }
      if (/^>\s?/.test(line)) {
        flush();
        var quote = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { quote.push(lines[i].replace(/^>\s?/, '')); i += 1; }
        out.push('<blockquote>' + quote.map(function (q) { return inline(esc(q)); }).join('<br>') + '</blockquote>');
        continue;
      }
      if (line.indexOf('|') >= 0 && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
        flush();
        var headers = splitRow(line);
        i += 2;
        var rows = [];
        while (i < lines.length && lines[i].indexOf('|') >= 0 && lines[i].trim()) {
          rows.push(splitRow(lines[i]));
          i += 1;
        }
        var table = ['<table><thead><tr>'];
        headers.forEach(function (cell) { table.push('<th>' + inline(esc(cell)) + '</th>'); });
        table.push('</tr></thead><tbody>');
        rows.forEach(function (cells) {
          table.push('<tr>');
          for (var c = 0; c < headers.length; c++) {
            table.push('<td>' + inline(esc(cells[c] == null ? '' : cells[c])) + '</td>');
          }
          table.push('</tr>');
        });
        table.push('</tbody></table>');
        out.push(table.join(''));
        continue;
      }
      if (/^\s*[-*+]\s+/.test(line)) {
        flush();
        var items = [];
        while (i < lines.length && /^\s*[-*+]\s+/.test(lines[i])) {
          items.push('<li>' + inline(esc(lines[i].replace(/^\s*[-*+]\s+/, ''))) + '</li>');
          i += 1;
        }
        out.push('<ul>' + items.join('') + '</ul>');
        continue;
      }
      if (/^\s*\d+[.)]\s+/.test(line)) {
        flush();
        var numbered = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) {
          numbered.push('<li>' + inline(esc(lines[i].replace(/^\s*\d+[.)]\s+/, ''))) + '</li>');
          i += 1;
        }
        out.push('<ol>' + numbered.join('') + '</ol>');
        continue;
      }
      if (!line.trim()) {
        flush();
        i += 1;
        continue;
      }
      paragraph.push(line);
      i += 1;
    }
    flush();
    return out.join('');
  }

  // ---------------- data ----------------
  function loadInfo() {
    return api('/api/info').then(function (info) {
      var name = info.agent || '智能体';
      byId('brandName').textContent = name;
      byId('topTitle').textContent = name;
      byId('brandAvatar').textContent = agentInitial();
      document.title = name + ' · 对话';
      return info;
    }).catch(function (error) {
      toast('无法读取 /api/info：' + error.message, true);
    });
  }

  function loadSessions(selectFirst) {
    return api('/v1/sessions').then(function (data) {
      state.sessions = data.sessions || [];
      renderSessions();
      var keep = false;
      for (var i = 0; i < state.sessions.length; i++) {
        if (state.sessions[i].id === state.currentId) { keep = true; }
      }
      if (!keep && (selectFirst || state.sessions.length) && state.sessions.length) {
        return selectSession(state.sessions[0].id);
      }
      if (!state.currentId) { renderMessages(); }
      return null;
    }).catch(function (error) {
      toast('无法读取会话：' + error.message, true);
    });
  }

  function loadFiles() {
    return api('/v1/files').then(function (data) {
      state.files = data.files || [];
      renderFiles();
    }).catch(function (error) {
      toast('无法读取文件：' + error.message, true);
    });
  }

  function selectSession(id) {
    return api('/v1/sessions/' + encodeURIComponent(id)).then(function (data) {
      state.currentId = id;
      state.messages = data.messages || [];
      renderSessions();
      renderMessages();
      return data;
    }).catch(function (error) {
      toast('无法打开会话：' + error.message, true);
    });
  }

  function createSession(title) {
    return api('/v1/sessions', { method: 'POST', body: { title: title || '' } }).then(function (data) {
      state.currentId = data.session.id;
      state.messages = [];
      renderMessages();
      return loadSessions(false).then(function () { return data.session.id; });
    });
  }

  function newSession() {
    if (state.busy) { return; }
    state.currentId = null;
    state.messages = [];
    renderMessages();
    renderSessions();
    byId('input').focus();
    closeRail();
  }

  function removeSession(id) {
    if (!window.confirm('删除这个会话？此操作不可恢复。')) { return; }
    api('/v1/sessions/' + encodeURIComponent(id), { method: 'DELETE' }).then(function () {
      if (state.currentId === id) {
        state.currentId = null;
        state.messages = [];
        renderMessages();
      }
      return loadSessions(true);
    }).catch(function (error) { toast('删除失败：' + error.message, true); });
  }

  // ---------------- streaming turn ----------------
  function readSse(response, onEvent) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder('utf-8');
    var buffer = '';
    function pump() {
      return reader.read().then(function (result) {
        if (result.done) { return; }
        buffer += decoder.decode(result.value, { stream: true });
        var boundary = buffer.indexOf('\n\n');
        while (boundary >= 0) {
          var frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          var lines = frame.split('\n');
          for (var i = 0; i < lines.length; i++) {
            var line = lines[i];
            if (line.indexOf('data:') === 0) {
              var payload = line.slice(5).trim();
              if (payload) {
                try { onEvent(JSON.parse(payload)); } catch (error) {}
              }
            }
          }
          boundary = buffer.indexOf('\n\n');
        }
        return pump();
      });
    }
    return pump();
  }

  function removeLastUser() {
    for (var i = state.messages.length - 1; i >= 0; i--) {
      if (state.messages[i].role === 'user') { state.messages.splice(i, 1); return; }
    }
  }

  function handleEvent(event) {
    if (!event || !event.type) { return; }
    if (event.type === 'done') {
      hideTyping();
      removeLastUser();
      if (event.user) { state.messages.push(event.user); }
      if (event.assistant) { state.messages.push(event.assistant); }
      renderMessages();
      if (event.session) { loadSessions(false); }
      return;
    }
    if (event.type === 'error') {
      hideTyping();
      var message = event.error || event.message || '运行失败';
      state.messages.push({ role: 'assistant', content: '运行失败：' + message, created_at: new Date().toISOString() });
      renderMessages();
      return;
    }
  }

  function finishTurn() {
    state.busy = false;
    state.controller = null;
    hideTyping();
    setSendState(false);
    loadSessions(false);
  }

  function stopTurn() {
    if (state.controller) { state.controller.abort(); }
  }

  function setSendState(sending) {
    var button = byId('send');
    button.innerHTML = '';
    if (sending) {
      button.className = 'send-btn stop-btn';
      button.title = '停止生成';
      button.innerHTML = svgIcon('M7 7h10v10H7z', 'icon');
    } else {
      button.className = 'send-btn';
      button.title = '发送';
      button.innerHTML = svgIcon('M12 19V5m0 0-6 6m6-6 6 6', 'icon');
    }
  }

  function deliver(sessionId, text, attachments) {
    state.busy = true;
    state.messages.push({ role: 'user', content: text, attachments: attachments, created_at: new Date().toISOString() });
    renderMessages();
    showTyping();
    setSendState(true);

    var controller = new AbortController();
    state.controller = controller;

    return fetch('/v1/sessions/' + encodeURIComponent(sessionId) + '/messages?stream=1', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ text: text, attachments: attachments }),
      signal: controller.signal
    }).then(function (response) {
      var contentType = response.headers.get('content-type') || '';
      if (!response.ok) {
        return response.text().then(function (raw) {
          var message = 'HTTP ' + response.status;
          try { message = JSON.parse(raw).error || message; } catch (error) {}
          throw new Error(message);
        });
      }
      if (contentType.indexOf('text/event-stream') >= 0 && response.body) {
        return readSse(response, handleEvent);
      }
      return response.json().then(function (data) {
        handleEvent({ type: 'done', user: data.user, assistant: data.assistant, output: data.output });
      });
    }).catch(function (error) {
      hideTyping();
      if (error && error.name === 'AbortError') {
        removeLastUser();
        renderMessages();
        toast('已停止本轮生成');
        return;
      }
      var message = (error && error.message) || '运行失败';
      state.messages.push({ role: 'assistant', content: '运行失败：' + message, created_at: new Date().toISOString() });
      renderMessages();
      toast('运行失败：' + message, true);
    }).then(function () { finishTurn(); });
  }

  function sendMessage() {
    if (state.busy) { stopTurn(); return; }
    var input = byId('input');
    var text = input.value.trim();
    if (!text && !state.pending.length) { return; }
    if (!text) { text = '请看看我上传的文件。'; }
    input.value = '';
    autoGrow();

    var attachments = state.pending.slice();
    state.pending = [];
    renderPending();

    var start = state.currentId ? Promise.resolve(state.currentId) : createSession(text.slice(0, 40));
    start.then(function (sessionId) {
      return deliver(sessionId, text, attachments);
    }).catch(function (error) {
      toast('发送失败：' + error.message, true);
      setSendState(false);
    });
  }

  function autoGrow() {
    var input = byId('input');
    input.style.height = 'auto';
    input.style.height = Math.min(180, input.scrollHeight) + 'px';
  }

  // ---------------- files ----------------
  function downloadFile(name) {
    fetch('/v1/files/' + encodeURIComponent(name).replace(/%2F/g, '/'), { headers: headers() })
      .then(function (response) {
        if (!response.ok) { throw new Error('HTTP ' + response.status); }
        return response.blob();
      })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        var anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = name.split('/').pop();
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        URL.revokeObjectURL(url);
      })
      .catch(function (error) { toast('下载失败：' + error.message, true); });
  }

  function uploadFiles(fileList) {
    var files = Array.prototype.slice.call(fileList || []);
    if (!files.length) { return; }
    files.forEach(function (file) {
      var reader = new FileReader();
      reader.onload = function () {
        var result = String(reader.result || '');
        var base64 = result.indexOf(',') >= 0 ? result.split(',')[1] : result;
        api('/v1/files', { method: 'POST', body: { name: file.name, content_base64: base64 } })
          .then(function (data) {
            state.pending.push({ name: data.file.name, size: data.file.size });
            renderPending();
            toast('已上传 ' + data.file.name);
            return loadFiles();
          })
          .catch(function (error) { toast('上传失败：' + error.message, true); });
      };
      reader.onerror = function () { toast('读取文件失败：' + file.name, true); };
      reader.readAsDataURL(file);
    });
  }

  // ---------------- token / theme / rail ----------------
  function openToken() {
    byId('tokenInput').value = state.token;
    byId('tokenModal').className = 'modal-mask open';
    byId('tokenInput').focus();
  }
  function closeToken() { byId('tokenModal').className = 'modal-mask'; }
  function saveToken() {
    state.token = byId('tokenInput').value.trim();
    try {
      if (state.token) { localStorage.setItem(TOKEN_KEY, state.token); }
      else { localStorage.removeItem(TOKEN_KEY); }
    } catch (error) {}
    closeToken();
    toast(state.token ? '令牌已保存' : '令牌已清除');
    loadSessions(false);
    loadFiles();
  }
  function clearToken() {
    byId('tokenInput').value = '';
    state.token = '';
    try { localStorage.removeItem(TOKEN_KEY); } catch (error) {}
    closeToken();
    toast('令牌已清除');
  }
  function openRail() { document.body.classList.add('rail-open'); }
  function closeRail() { document.body.classList.remove('rail-open'); }

  function init() {
    try {
      state.token = localStorage.getItem(TOKEN_KEY) || '';
      state.theme = localStorage.getItem(THEME_KEY) || 'light';
    } catch (error) { state.token = ''; state.theme = 'light'; }
    // `?theme=light|dark` overrides the saved preference for this load only.
    // Used by embeds and by screenshots; it deliberately does not persist.
    var forced = /[?&]theme=(light|dark)/.exec(window.location.search);
    if (forced) { state.theme = forced[1]; }
    applyTheme();

    byId('newSession').onclick = newSession;
    byId('refresh').onclick = function () { loadInfo(); loadSessions(false); loadFiles(); };
    byId('themeToggle').onclick = cycleTheme;
    byId('settingsBtn').onclick = openToken;
    byId('tokenSave').onclick = saveToken;
    byId('tokenClear').onclick = clearToken;
    byId('tokenModal').onclick = function (event) {
      if (event.target === byId('tokenModal')) { closeToken(); }
    };
    byId('send').onclick = sendMessage;
    byId('attachBtn').onclick = function () { byId('file').click(); };
    byId('railToggle').onclick = openRail;
    byId('file').onchange = function (event) {
      uploadFiles(event.target.files);
      event.target.value = '';
    };
    byId('input').addEventListener('input', autoGrow);
    byId('input').addEventListener('keydown', function (event) {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendMessage();
      }
    });
    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape') { closeToken(); closeRail(); }
    });
    document.addEventListener('click', function (event) {
      if (document.body.classList.contains('rail-open')) {
        var rail = byId('rail');
        var toggle = byId('railToggle');
        if (rail.contains(event.target) === false && toggle.contains(event.target) === false) { closeRail(); }
      }
    });

    var thread = byId('thread');
    thread.addEventListener('dragover', function (event) {
      event.preventDefault();
      byId('dropOverlay').className = 'drop-overlay show';
    });
    thread.addEventListener('dragleave', function () {
      byId('dropOverlay').className = 'drop-overlay';
    });
    thread.addEventListener('drop', function (event) {
      event.preventDefault();
      byId('dropOverlay').className = 'drop-overlay';
      uploadFiles(event.dataTransfer.files);
    });

    loadInfo();
    loadSessions(true);
    loadFiles();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# The request handler
# ---------------------------------------------------------------------------


class _AgentRequestHandler(BaseHTTPRequestHandler):
    """One request per thread. ``agent`` is injected by :class:`AgentHTTPServer`.

    Routed by path segments rather than regexes: the paths are few and every
    segment is unquoted exactly once, which is also what keeps an encoded ``..``
    from being mistaken for a route.
    """

    agent: "AgentHTTPServer"
    server_version = "AgentHTTPServer/1.0"

    # -- logging ------------------------------------------------------------
    def log_message(self, message_format: str, *args: Any) -> None:
        """Access log on stderr. Responses never contain a traceback - only this
        log does."""
        print(
            f"[agent-http] {self.address_string()} {message_format % args}",
            file=sys.stderr,
            flush=True,
        )

    # -- verbs --------------------------------------------------------------
    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_PATCH(self) -> None:
        self._handle("PATCH")

    def do_OPTIONS(self) -> None:
        self._handle("OPTIONS")

    # -- dispatch -----------------------------------------------------------
    def _handle(self, method: str) -> None:
        try:
            self._route(method)
        except _HTTPError as error:
            self._safe_error(error.status, error.message)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # noqa: BLE001 - a response, not a crash, is the contract
            traceback.print_exc(file=sys.stderr)
            self._safe_error(500, "internal server error")

    def _route(self, method: str) -> None:
        parsed = urlsplit(self.path)
        segments = [unquote(part) for part in parsed.path.split("/") if part]
        query = parse_qs(parsed.query)
        cors = segments[:1] == ["v1"]

        if method == "OPTIONS":
            # Preflight must succeed without the token: a browser does not attach
            # Authorization to it.
            self._send_empty(204, cors=cors)
            return

        if self._protected(segments) and not self._authorized():
            self._send_json(
                401,
                {"ok": False, "error": "unauthorized: send 'Authorization: Bearer <token>'"},
                cors=cors,
            )
            return

        if method == "GET":
            self._get(segments, cors)
        elif method == "POST":
            self._post(segments, query, cors)
        elif method == "DELETE":
            self._delete(segments, cors)
        else:
            self._send_json(
                405,
                {"ok": False, "error": f"method {method} is not allowed"},
                cors=cors,
            )

    # -- GET ----------------------------------------------------------------
    def _get(self, segments: list[str], cors: bool) -> None:
        if not segments:
            self._send_chat_page()
            return
        if segments == ["healthz"]:
            self._send_json(200, {"status": "ok"})
            return
        if segments == ["api", "info"]:
            self._send_json(200, self.agent.info(), cors=cors)
            return
        if segments[:2] == ["v1", "sessions"]:
            service = self._require_sessions()
            if len(segments) == 2:
                self._send_json(
                    200,
                    {"sessions": [s.to_dict() for s in service.list_sessions()]},
                    cors=cors,
                )
                return
            if len(segments) == 3:
                self._send_session_detail(service, segments[2], cors)
                return
            raise _HTTPError(404, "not found")
        if segments[:2] == ["v1", "files"]:
            workspace = self.agent.workspace()
            if len(segments) == 2:
                self._send_json(200, {"files": workspace.list()}, cors=cors)
                return
            self._download(workspace, "/".join(segments[2:]))
            return
        raise _HTTPError(404, "not found")

    def _send_chat_page(self) -> None:
        if not self.agent.config.chat_page:
            self._send_json(
                200,
                {
                    "ok": True,
                    "agent": self.agent.config.agent_name or "agent",
                    "chat_page": False,
                },
            )
            return
        body = CHAT_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_session_detail(
        self, service: SessionService, session_id: str, cors: bool
    ) -> None:
        try:
            session = service.get_session(session_id)
            messages = service.session_messages(session_id)
        except SessionNotFound as error:
            raise _HTTPError(404, str(error)) from error
        self._send_json(
            200,
            {
                "session": session.to_dict(),
                "messages": [message.to_dict() for message in messages],
            },
            cors=cors,
        )

    def _download(self, workspace: Workspace, name: str) -> None:
        path = self._workspace_path(workspace, name)
        if not path.is_file():
            raise _HTTPError(404, f"'{name}' does not exist in the workspace")
        try:
            data = workspace.read_bytes(name)
        except WorkspaceError as error:
            # The workspace refuses oversized reads; that is a 413, not a 500.
            raise _HTTPError(413, str(error)) from error
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", _content_disposition(name))
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    # -- POST ---------------------------------------------------------------
    def _post(self, segments: list[str], query: dict[str, list[str]], cors: bool) -> None:
        if segments == ["v1", "invoke"]:
            self._invoke()
            return
        if segments == ["v1", "sessions"]:
            self._create_session()
            return
        if (
            len(segments) == 4
            and segments[:2] == ["v1", "sessions"]
            and segments[3] == "messages"
        ):
            self._post_message(segments[2], query)
            return
        if segments == ["v1", "files"]:
            self._upload()
            return
        raise _HTTPError(404, "not found")

    def _invoke(self) -> None:
        body = self._read_json()
        inputs = body.get("input", {})
        if inputs is None:
            inputs = {}
        if not isinstance(inputs, dict):
            raise _HTTPError(400, "'input' must be a JSON object")
        try:
            output = self.agent.run_inputs(inputs)
        except Exception as error:  # noqa: BLE001 - reported as a JSON 500
            traceback.print_exc(file=sys.stderr)
            raise _HTTPError(
                500, f"run failed: {type(error).__name__}: {error}"
            ) from error
        self._send_json(200, {"ok": True, "output": output}, cors=True)

    def _create_session(self) -> None:
        service = self._require_sessions()
        body = self._read_json()
        title = body.get("title", "")
        metadata = body.get("metadata", {})
        if not isinstance(title, str):
            raise _HTTPError(400, "'title' must be a string")
        if not isinstance(metadata, dict):
            raise _HTTPError(400, "'metadata' must be an object")
        try:
            session = service.create_session(title=title, metadata=metadata)
        except ValueError as error:
            raise _HTTPError(400, str(error)) from error
        self._send_json(201, {"session": session.to_dict()}, cors=True)

    def _post_message(
        self, session_id: str, query: dict[str, list[str]]
    ) -> None:
        service = self._require_sessions()
        body = self._read_json()
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise _HTTPError(400, "'text' is required and must be a non-empty string")
        attachments = body.get("attachments") or []
        if not isinstance(attachments, list) or any(
            not isinstance(item, dict) for item in attachments
        ):
            raise _HTTPError(400, "'attachments' must be a list of objects")
        stream = (query.get("stream") or [""])[0].strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        try:
            service.get_session(session_id)
        except SessionNotFound as error:
            raise _HTTPError(404, str(error)) from error

        lock = self.agent.lock_session(session_id)
        if stream:
            self._stream_message(lock, service, session_id, text, attachments)
            return
        try:
            with lock:
                result = asyncio.run(
                    service.post_message(
                        session_id, text, attachments=attachments
                    )
                )
        except SessionNotFound as error:
            raise _HTTPError(404, str(error)) from error
        except ValueError as error:
            raise _HTTPError(400, str(error)) from error
        except Exception as error:  # noqa: BLE001 - reported as a JSON 500
            traceback.print_exc(file=sys.stderr)
            raise _HTTPError(
                500, f"run failed: {type(error).__name__}: {error}"
            ) from error
        self._send_json(
            200,
            {
                "ok": True,
                "user": result["user"].to_dict(),
                "assistant": result["assistant"].to_dict(),
                "output": result["output"],
            },
            cors=True,
        )

    def _stream_message(
        self,
        lock: threading.Lock,
        service: SessionService,
        session_id: str,
        text: str,
        attachments: list[dict[str, Any]],
    ) -> None:
        """Server-sent events: progress frames from ``emit``, then one ``done``.

        The status line is sent before the run starts, so a failure from here on
        is an ``error`` frame rather than a 500 - the client already has a 200.
        A client that disconnects mid-run must not turn into a recorded failure:
        the ``emit`` wrapper swallows the broken pipe and the turn completes, so
        the transcript keeps the answer the next reader would have seen.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()

        closed = {"value": False}

        def emit(event: dict[str, Any]) -> None:
            if closed["value"]:
                return
            payload = (
                event
                if isinstance(event, dict) and "type" in event
                else {"type": "progress", "data": event}
            )
            try:
                self._write_sse(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                closed["value"] = True

        with lock:
            try:
                result = asyncio.run(
                    service.post_message(
                        session_id, text, attachments=attachments, emit=emit
                    )
                )
                if not closed["value"]:
                    self._write_sse(
                        {
                            "type": "done",
                            "user": result["user"].to_dict(),
                            "assistant": result["assistant"].to_dict(),
                            "output": result["output"],
                        }
                    )
            except SessionNotFound as error:
                if not closed["value"]:
                    self._write_sse({"type": "error", "error": str(error)})
            except Exception as error:  # noqa: BLE001 - the stream reports it
                traceback.print_exc(file=sys.stderr)
                if not closed["value"]:
                    self._write_sse(
                        {"type": "error", "error": f"{type(error).__name__}: {error}"}
                    )
            finally:
                self.close_connection = True

    def _upload(self) -> None:
        body = self._read_json()
        name = body.get("name")
        content_base64 = body.get("content_base64")
        if not isinstance(name, str) or not name.strip():
            raise _HTTPError(400, "'name' is required and must be a non-empty string")
        if not isinstance(content_base64, str):
            raise _HTTPError(400, "'content_base64' is required and must be a string")
        try:
            data = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise _HTTPError(400, "'content_base64' is not valid base64") from error
        limit = self.agent.config.max_body_bytes
        if limit and len(data) > limit:
            raise _HTTPError(413, f"file is {len(data)} bytes; the limit is {limit}")
        workspace = self.agent.workspace()
        if len(data) > workspace.max_write_bytes:
            raise _HTTPError(
                413,
                f"file is {len(data)} bytes; this workspace allows "
                f"{workspace.max_write_bytes}",
            )
        try:
            workspace.ensure_capacity()
            info = workspace.write_bytes(name, data)
        except WorkspaceError as error:
            raise _HTTPError(400, str(error)) from error
        self._send_json(201, {"ok": True, "file": info}, cors=True)

    # -- DELETE -------------------------------------------------------------
    def _delete(self, segments: list[str], cors: bool) -> None:
        if len(segments) == 3 and segments[:2] == ["v1", "sessions"]:
            service = self._require_sessions()
            if service.delete_session(segments[2]):
                self._send_json(200, {"ok": True}, cors=cors)
            else:
                self._send_json(
                    404, {"ok": False, "error": "session not found"}, cors=cors
                )
            return
        if len(segments) >= 3 and segments[:2] == ["v1", "files"]:
            workspace = self.agent.workspace()
            name = "/".join(segments[2:])
            path = self._workspace_path(workspace, name)
            if not path.is_file():
                raise _HTTPError(404, f"'{name}' does not exist in the workspace")
            try:
                workspace.delete(name)
            except WorkspaceError as error:
                raise _HTTPError(400, str(error)) from error
            self._send_json(200, {"ok": True}, cors=cors)
            return
        raise _HTTPError(404, "not found")

    # -- responses ----------------------------------------------------------
    def _send_json(self, status: int, payload: Any, *, cors: bool = False) -> None:
        body = _dumps(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cors:
            self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int, *, cors: bool = False) -> None:
        self.send_response(status)
        if cors:
            self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _safe_error(self, status: int, message: str) -> None:
        """Best-effort error response: a client that has gone away is not a bug."""
        try:
            parsed = urlsplit(self.path)
            segments = [unquote(part) for part in parsed.path.split("/") if part]
            self._send_json(
                status, {"ok": False, "error": message}, cors=segments[:1] == ["v1"]
            )
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close_connection = True

    def _write_sse(self, payload: dict[str, Any]) -> None:
        frame = "data: " + json.dumps(
            payload, ensure_ascii=False, default=str
        ) + "\n\n"
        self.wfile.write(frame.encode("utf-8"))
        self.wfile.flush()

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", CORS_ALLOW_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", CORS_ALLOW_METHODS)
        self.send_header("Access-Control-Allow-Headers", CORS_ALLOW_HEADERS)
        self.send_header("Access-Control-Max-Age", CORS_MAX_AGE)

    # -- request helpers ----------------------------------------------------
    def _read_body(self) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b""
        try:
            length = int(raw_length)
        except (TypeError, ValueError) as error:
            raise _HTTPError(400, "invalid Content-Length header") from error
        if length < 0:
            raise _HTTPError(400, "invalid Content-Length header")
        limit = self.agent.config.max_body_bytes
        if limit and length > limit:
            raise _HTTPError(
                413, f"request body is {length} bytes; the limit is {limit}"
            )
        return self.rfile.read(length)

    def _read_json(self) -> dict[str, Any]:
        body = self._read_body()
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise _HTTPError(400, "request body is not valid JSON") from error
        if not isinstance(parsed, dict):
            raise _HTTPError(400, "request body must be a JSON object")
        return parsed

    def _protected(self, segments: list[str]) -> bool:
        return segments[:1] == ["v1"] or segments[:2] == ["api", "info"]

    def _authorized(self) -> bool:
        token = self.agent.config.token
        if not token:
            return True
        header = self.headers.get("Authorization", "").strip()
        try:
            return hmac.compare_digest(header, f"Bearer {token}")
        except TypeError:  # a non-ASCII header value cannot match
            return False

    def _require_sessions(self) -> SessionService:
        service = self.agent.session_service
        if service is None:
            raise _HTTPError(503, "sessions are not enabled on this server")
        return service

    @staticmethod
    def _workspace_path(workspace: Workspace, name: str) -> Any:
        try:
            return workspace.resolve(name)
        except WorkspaceError as error:
            # Traversal, absolute paths, reserved names: a bad request, not a 404
            # (the client needs to know it is not allowed, not that it vanished).
            raise _HTTPError(400, str(error)) from error


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class AgentHTTPServer:
    """A threaded, optionally token-guarded HTTP server around ``run_fn``.

    ``session_service`` is optional: without one the server still answers
    ``/healthz``, ``/``, ``/api/info``, ``/v1/invoke`` and the file routes, and
    reports ``sessions: false`` - every ``/v1/sessions*`` route then answers 503.
    An exported ``main.py`` always supplies one backed by
    :class:`~app.runtime.session.SQLiteSessionStore`.

    ``workspace_factory`` is a zero-argument callable returning a
    :class:`~app.runtime.workspace.Workspace`; the default resolves one from the
    environment on every request, which is what lets a deployment move its
    workspace with ``AGENT_WORKSPACE_DIR`` and a test point it at ``tmp_path``.
    """

    def __init__(
        self,
        *,
        run_fn: Callable[..., Any],
        session_service: SessionService | None = None,
        workspace_factory: Callable[[], Workspace] | None = None,
        config: HTTPConfig | None = None,
    ) -> None:
        self.run_fn = run_fn
        self.session_service = session_service
        self.workspace_factory = workspace_factory
        self.config = config or HTTPConfig()
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.Lock()
        self._started = threading.Event()

        # A per-server handler subclass carries the agent instance, so two
        # servers in one process (the test suite does this) never share state.
        handler_class = type(
            "BoundAgentRequestHandler",
            (_AgentRequestHandler,),
            {"agent": self},
        )
        self._httpd = ThreadingHTTPServer(
            (self.config.host, self.config.port), handler_class
        )
        self._httpd.daemon_threads = True

    # -- addresses ----------------------------------------------------------
    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[:2]
        text_host = str(host)
        if text_host in ("0.0.0.0", "::", ""):
            text_host = "127.0.0.1"
        return f"http://{text_host}:{port}"

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    # -- lifecycle ----------------------------------------------------------
    def serve_forever(self) -> None:
        """Serve until :meth:`shutdown`. Blocks; run it in a thread if needed.

        The poll interval is short on purpose: shutdown waits for one poll, and a
        process that takes half a second to stop on Ctrl-C feels broken. The cost
        of a 50 ms wakeup is irrelevant next to the work a request does.
        """
        self._started.set()
        try:
            self._httpd.serve_forever(poll_interval=0.05)
        finally:
            self._started.clear()

    def start(self) -> threading.Thread:
        """Serve in a daemon thread and return it (tests, or an embedding app)."""
        thread = threading.Thread(
            target=self.serve_forever, name="agent-http", daemon=True
        )
        thread.start()
        self._started.wait(timeout=5.0)
        return thread

    def shutdown(self) -> None:
        """Stop :meth:`serve_forever`. Safe to call from another thread."""
        self._httpd.shutdown()

    def server_close(self) -> None:
        self._httpd.server_close()

    def close(self) -> None:
        """Stop serving (if started) and release the socket. Idempotent enough."""
        if self._started.is_set():
            self._httpd.shutdown()
        self._httpd.server_close()
    def __enter__(self) -> "AgentHTTPServer":
        self.start()
        return self

    def __exit__(self, *_exc_info: Any) -> None:
        self.close()

    # -- the services the handler reaches for -------------------------------
    def workspace(self) -> Workspace:
        if self.workspace_factory is None:
            return workspace_from_env()
        return self.workspace_factory()

    def info(self) -> dict[str, Any]:
        return {
            "agent": self.config.agent_name or "agent",
            "sessions": self.session_service is not None,
            "files": True,
        }

    def run_inputs(self, inputs: dict[str, Any]) -> Any:
        """Run the agent once from a request thread (one fresh event loop)."""

        async def _invoke() -> Any:
            return await self.run_fn(inputs)

        return asyncio.run(_invoke())

    def lock_session(self, session_id: str) -> threading.Lock:
        """The per-session thread lock.

        ``SessionService`` already serialises a conversation with an asyncio
        lock, but each request here runs ``asyncio.run`` - a *different* event
        loop - so that lock alone would not order two request threads. This one
        does, and it is per session: different conversations still run in
        parallel.
        """
        with self._session_locks_guard:
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._session_locks[session_id] = lock
            return lock


def serve_http(
    run_fn: Callable[..., Any],
    *,
    session_service: SessionService | None = None,
    workspace_factory: Callable[[], Workspace] | None = None,
    config: HTTPConfig | None = None,
) -> None:
    """Build a server and serve it until interrupted.

    This is the whole body of the exported ``main.py``'s ``serve-http`` command:
    the template knows how to compile its graph and hold a checkpointer, and this
    function knows how to put an HTTP face on it.
    """
    server = AgentHTTPServer(
        run_fn=run_fn,
        session_service=session_service,
        workspace_factory=workspace_factory,
        config=config,
    )
    try:
        print(f"agent http server listening on {server.url}", file=sys.stderr, flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
