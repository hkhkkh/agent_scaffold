"""Business integration nodes: API, FHIR, SQL, email, documents.

These exist because "draw a workflow" is not the hard part of a real
deployment — reaching the customer's systems is. The built-in `http_request`
tool covers a single unauthenticated GET/POST; it cannot refresh an OAuth2
token, page through a FHIR Bundle, run a read-only SQL query, send mail, or
extract text from a PDF. Those are the four shapes almost every back-office
agent needs, so they are nodes rather than one-off scripts.

Two rules apply to all of them:

* **Secrets can be referenced by environment variable.** A value beginning with
  ``env:`` is replaced with ``os.environ[NAME]`` at run time, so the stored graph
  carries ``env:FHIR_CLIENT_SECRET`` rather than the secret itself.
* **Outbound HTTP goes through the existing egress policy** (`http_request`'s
  helper), so a workflow cannot use these nodes to reach link-local metadata or
  an unlisted host.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import smtplib
import sqlite3
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import httpx

from app.graph.schema import (
    BooleanParam,
    InputPort,
    JsonParam,
    NodeCategory,
    NodeTypeSpec,
    NumberParam,
    OutputPort,
    PortType,
    SelectOption,
    SelectParam,
    StringParam,
)
from app.nodes.base import BaseNode, NodeContext, template_scope
from app.nodes.registry import register_node
from app.runtime.egress import get_egress_policy
from app.runtime.template import render
from app.tools.builtin import _http_request

#: Node types that must not run in the shared public demo. They can reach a
#: private network, send mail, or read files — all of which belong to a private
#: deployment with credentials, not to an anonymous visitor.
PUBLIC_DEMO_DISABLED_TYPES = frozenset(
    {"fhir_query", "sql_query", "email_send", "document_extract"}
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _secret(value: Any) -> str:
    """Resolve ``env:NAME`` so a graph never has to store the secret itself."""
    text = "" if value is None else str(value)
    if text.startswith("env:"):
        return os.environ.get(text[4:].strip(), "")
    return text


def _fill(value: Any, scope: dict[str, Any]) -> Any:
    """Resolve ``{{ }}`` placeholders recursively (same rule as the Tool node)."""
    if isinstance(value, str):
        return render(value, scope)
    if isinstance(value, dict):
        return {key: _fill(item, scope) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, scope) for item in value]
    return value


def _guard_public(ctx: NodeContext, label: str) -> None:
    if ctx.settings.public_demo:
        raise ValueError(
            f"{label} is disabled in the public demo. It needs a private "
            "deployment with credentials and network access."
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _auth_select() -> list[SelectOption]:
    return [
        SelectOption(label="None", label_zh="无", value="none"),
        SelectOption(label="Bearer token", label_zh="Bearer Token", value="bearer"),
        SelectOption(label="Basic auth", label_zh="Basic 认证", value="basic"),
        SelectOption(label="API key", label_zh="API Key", value="api_key"),
        SelectOption(
            label="OAuth2 client credentials",
            label_zh="OAuth2 客户端凭据",
            value="oauth2",
        ),
    ]


async def _oauth2_token(config: dict[str, Any], *, timeout: float) -> str:
    token_url = str(config.get("oauth_token_url") or "").strip()
    client_id = str(config.get("oauth_client_id") or "").strip()
    client_secret = _secret(config.get("oauth_client_secret"))
    if not token_url or not client_id or not client_secret:
        raise ValueError(
            "OAuth2 needs token URL, client id and client secret "
            "(use env:NAME for the secret)."
        )

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    form: dict[str, Any] = {"grant_type": "client_credentials"}
    scope = str(config.get("oauth_scope") or "").strip()
    if scope:
        form["scope"] = scope

    response = await _http_request(
        token_url,
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Accept": "application/json",
        },
        form=form,
        timeout=timeout,
    )
    if response.get("error"):
        raise ValueError(f"OAuth2 token request failed: {response.get('detail')}")
    if not response.get("ok"):
        raise ValueError(
            f"OAuth2 token endpoint returned HTTP {response.get('status')}: "
            f"{str(response.get('body'))[:300]}"
        )
    body = response.get("body")
    token = body.get("access_token") if isinstance(body, dict) else None
    if not token:
        raise ValueError("OAuth2 token response did not contain access_token.")
    return str(token)


async def _auth(
    config: dict[str, Any],
    *,
    timeout: float,
    query: dict[str, Any],
    headers: dict[str, str],
) -> None:
    """Apply the configured auth to `headers` / `query` in place."""
    kind = str(config.get("auth_kind") or "none")
    if kind == "bearer":
        token = _secret(config.get("bearer_token"))
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif kind == "basic":
        user = str(config.get("basic_username") or "")
        password = _secret(config.get("basic_password"))
        if user or password:
            encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
    elif kind == "api_key":
        name = str(config.get("api_key_name") or "X-API-Key")
        value = _secret(config.get("api_key_value"))
        if value:
            if str(config.get("api_key_location") or "header") == "query":
                query[name] = value
            else:
                headers[name] = value
    elif kind == "oauth2":
        token = await _oauth2_token(config, timeout=timeout)
        headers["Authorization"] = f"Bearer {token}"


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------


API_SPEC = NodeTypeSpec(
    type="api_call",
    label="API call",
    label_zh="API 调用",
    category=NodeCategory.INTEGRATION,
    description="Call a REST API with authentication and return its response.",
    description_zh="带鉴权调用 REST API，并返回响应内容。",
    icon="globe",
    color="#0ea5e9",
    inputs=[
        InputPort(
            id="in",
            label="Context",
            label_zh="上下文",
            type=PortType.ANY,
            description="Supplies template variables for the URL, headers and body.",
            description_zh="为 URL、请求头和请求体提供模板变量。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Response", label_zh="响应", type=PortType.JSON),
        OutputPort(id="text", label="Text", label_zh="文本", type=PortType.TEXT),
        OutputPort(id="status", label="HTTP status", label_zh="状态码", type=PortType.NUMBER),
    ],
    params=[
        StringParam(
            name="url",
            label="URL",
            label_zh="URL",
            description=(
                "Absolute URL. May contain {{ }} placeholders. The host must be "
                "permitted by the deployment's egress policy."
            ),
            description_zh=(
                "完整 URL，可用 {{ }} 占位符。主机必须在本部署的出网策略允许范围内。"
            ),
            required=True,
        ),
        SelectParam(
            name="method",
            label="Method",
            label_zh="方法",
            default="GET",
            options=[
                SelectOption(label=verb, value=verb)
                for verb in ("GET", "POST", "PUT", "PATCH", "DELETE")
            ],
        ),
        JsonParam(
            name="headers",
            label="Headers",
            label_zh="请求头",
            description="JSON object of request headers.",
            description_zh="请求头的 JSON 对象。",
            default={},
            rows=4,
        ),
        JsonParam(
            name="params",
            label="Query parameters",
            label_zh="查询参数",
            description="JSON object appended to the URL as a query string.",
            description_zh="作为查询字符串拼到 URL 上的 JSON 对象。",
            default={},
            rows=4,
        ),
        JsonParam(
            name="body",
            label="JSON body",
            label_zh="JSON 请求体",
            description="Sent as JSON for POST/PUT/PATCH.",
            description_zh="POST/PUT/PATCH 时作为 JSON 发送。",
            default={},
            rows=6,
        ),
        SelectParam(
            name="auth_kind",
            label="Authentication",
            label_zh="认证方式",
            default="none",
            options=_auth_select(),
        ),
        StringParam(
            name="bearer_token",
            label="Bearer token",
            label_zh="Bearer Token",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        StringParam(name="basic_username", label="Basic username", label_zh="Basic 用户名"),
        StringParam(
            name="basic_password",
            label="Basic password",
            label_zh="Basic 密码",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        StringParam(
            name="api_key_name",
            label="API key name",
            label_zh="API Key 名称",
            default="X-API-Key",
        ),
        StringParam(
            name="api_key_value",
            label="API key value",
            label_zh="API Key 值",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        SelectParam(
            name="api_key_location",
            label="API key location",
            label_zh="API Key 位置",
            default="header",
            options=[
                SelectOption(label="Header", label_zh="请求头", value="header"),
                SelectOption(label="Query", label_zh="查询参数", value="query"),
            ],
        ),
        StringParam(
            name="oauth_token_url",
            label="OAuth2 token URL",
            label_zh="OAuth2 Token 地址",
        ),
        StringParam(name="oauth_client_id", label="OAuth2 client id", label_zh="OAuth2 客户端 ID"),
        StringParam(
            name="oauth_client_secret",
            label="OAuth2 client secret",
            label_zh="OAuth2 客户端密钥",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        StringParam(name="oauth_scope", label="OAuth2 scope", label_zh="OAuth2 Scope"),
        NumberParam(
            name="timeout",
            label="Timeout (seconds)",
            label_zh="超时（秒）",
            default=30,
            min=1,
            max=300,
        ),
        BooleanParam(
            name="fail_on_error",
            label="Fail the run on HTTP error",
            label_zh="HTTP 出错时让运行失败",
            default=True,
        ),
    ],
    ai_notes="""\
Use this node when the business system exposes a REST API and the call is
deterministic. It is the general-purpose connector; prefer the narrower nodes
when they fit: `fhir_query` for FHIR, `sql_query` for databases, `email_send`
for mail, and the Tool node's `web_search` for public research.

Authentication: `none`, `bearer`, `basic`, `api_key` (header or query) or
`oauth2` client-credentials. OAuth2 fetches a fresh token on every run; it is not
cached between runs. Secrets may be written as `env:NAME`, which reads the
environment variable at run time — prefer that over pasting a secret into the
node.

`out` is the parsed JSON body when the response is JSON, otherwise the text;
`status` is the HTTP status. When `fail_on_error` is off, a failure returns
`{"error": ...}` on `out` and the run continues. The URL must pass the egress
policy, so an unlisted host is refused rather than silently attempted.

Do not use this to log in to a website, drive a browser, or submit forms on a
site that has no API. It sends one HTTP request; it does not maintain a browser
session, execute JavaScript, or solve a captcha.\
""",
)


@register_node
class ApiCallNode(BaseNode):
    spec = API_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        config = self.resolve_config(ctx)
        scope = template_scope(ctx, config)
        url = str(_fill(config.get("url") or "", scope)).strip()
        if not url:
            raise ValueError("API call has no URL.")
        method = str(config.get("method") or "GET").upper()
        headers = _fill(config.get("headers") or {}, scope)
        query = _fill(config.get("params") or {}, scope)
        body = _fill(config.get("body") or {}, scope)
        timeout = float(config.get("timeout") or 30)

        headers = {str(k): str(v) for k, v in (headers or {}).items()}
        query = {str(k): v for k, v in (query or {}).items()}
        await _auth(config, timeout=timeout, query=query, headers=headers)

        response = await _http_request(
            url,
            method=method,
            headers=headers,
            params=query,
            body=body,
            timeout=timeout,
        )
        status = int(response.get("status") or 0)
        text = str(response.get("body") if not isinstance(response.get("body"), dict) else json.dumps(response.get("body"), ensure_ascii=False))
        ok = bool(response.get("ok"))

        if response.get("error"):
            message = f"API call failed: {response.get('detail') or response['error']}"
            await ctx.log(message, "error")
            if config.get("fail_on_error", True):
                raise ValueError(message)
            return {"out": {"error": response["error"], "detail": response.get("detail")}, "text": message, "status": status}
        if not ok and config.get("fail_on_error", True):
            message = f"API returned HTTP {status}: {text[:500]}"
            await ctx.log(message, "error")
            raise ValueError(message)

        await ctx.log(f"API call {method} {url} → HTTP {status}.", "info")
        return {"out": response.get("body"), "text": text, "status": status}


# ---------------------------------------------------------------------------
# FHIR
# ---------------------------------------------------------------------------


FHIR_SPEC = NodeTypeSpec(
    type="fhir_query",
    label="FHIR query",
    label_zh="FHIR 查询",
    category=NodeCategory.INTEGRATION,
    description="Read FHIR R4 resources with search parameters and Bundle paging.",
    description_zh="按查询参数读取 FHIR R4 资源，并自动翻页 Bundle。",
    icon="heart-pulse",
    color="#f43f5e",
    inputs=[
        InputPort(
            id="in",
            label="Context",
            label_zh="上下文",
            type=PortType.ANY,
            description="Supplies template variables for the base URL and search parameters.",
            description_zh="为 base URL 和查询参数提供模板变量。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Resources", label_zh="资源列表", type=PortType.JSON),
        OutputPort(id="text", label="Text", label_zh="文本", type=PortType.TEXT),
        OutputPort(id="count", label="Count", label_zh="数量", type=PortType.NUMBER),
    ],
    params=[
        StringParam(
            name="base_url",
            label="FHIR base URL",
            label_zh="FHIR 地址",
            description="For example https://fhir.example.org/r4 (no trailing slash).",
            description_zh="例如 https://fhir.example.org/r4（末尾不要斜杠）。",
            required=True,
        ),
        StringParam(
            name="resource",
            label="Resource type",
            label_zh="资源类型",
            description="Patient, Encounter, Observation, DiagnosticReport, ...",
            description_zh="Patient、Encounter、Observation、DiagnosticReport 等。",
            default="Patient",
            required=True,
        ),
        JsonParam(
            name="search",
            label="Search parameters",
            label_zh="查询参数",
            description='JSON object, e.g. {"patient": "123", "_sort": "-date"}.',
            description_zh='JSON 对象，例如 {"patient": "123", "_sort": "-date"}。',
            default={},
            rows=5,
        ),
        SelectParam(
            name="auth_kind",
            label="Authentication",
            label_zh="认证方式",
            default="none",
            options=[
                SelectOption(label="None", label_zh="无", value="none"),
                SelectOption(label="Bearer token", label_zh="Bearer Token", value="bearer"),
                SelectOption(
                    label="OAuth2 client credentials",
                    label_zh="OAuth2 客户端凭据",
                    value="oauth2",
                ),
            ],
        ),
        StringParam(
            name="bearer_token",
            label="Bearer token",
            label_zh="Bearer Token",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        StringParam(name="oauth_token_url", label="OAuth2 token URL", label_zh="OAuth2 Token 地址"),
        StringParam(name="oauth_client_id", label="OAuth2 client id", label_zh="OAuth2 客户端 ID"),
        StringParam(
            name="oauth_client_secret",
            label="OAuth2 client secret",
            label_zh="OAuth2 客户端密钥",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        StringParam(name="oauth_scope", label="OAuth2 scope", label_zh="OAuth2 Scope"),
        NumberParam(
            name="page_size",
            label="Page size",
            label_zh="每页数量",
            default=50,
            min=1,
            max=500,
        ),
        NumberParam(
            name="max_pages",
            label="Max pages",
            label_zh="最多页数",
            default=5,
            min=1,
            max=50,
        ),
        NumberParam(
            name="timeout",
            label="Timeout (seconds)",
            label_zh="超时（秒）",
            default=30,
            min=1,
            max=300,
        ),
        BooleanParam(
            name="fail_on_error",
            label="Fail the run on error",
            label_zh="出错时让运行失败",
            default=True,
        ),
    ],
    ai_notes="""\
Use this instead of `api_call` when the source is a FHIR R4 server: it builds
`{base_url}/{resource}` with the search parameters, sets
`Accept: application/fhir+json`, and follows Bundle `next` links up to
`max_pages`. `out` is the flattened array of resources (not the Bundle), so a
downstream LLM can read `out.0.name.0.text` for a Patient or
`out.0.valueQuantity.value` for an Observation.

This node is **read-only** (GET only). Do not use it to create or update
resources. For a hospital integration, the deployment must allow the FHIR host
in its egress policy and must be a private instance — patient data must not be
sent to a public demo or an unapproved model provider.

Search parameters are ordinary FHIR query parameters, one key per parameter.
`_count` is set from `page_size`; `_sort` and `_include` may be included in
`search`. Paging stops at `max_pages` and reports `count`; a truncated result is
not an error.\
""",
)


@register_node
class FhirQueryNode(BaseNode):
    spec = FHIR_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        _guard_public(ctx, "FHIR query")
        config = self.resolve_config(ctx)
        scope = template_scope(ctx, config)
        base = str(_fill(config.get("base_url") or "", scope)).strip().rstrip("/")
        resource = str(_fill(config.get("resource") or "", scope)).strip().strip("/")
        if not base or not resource:
            raise ValueError("FHIR query needs a base URL and a resource type.")
        search = _fill(config.get("search") or {}, scope)
        params = {str(k): v for k, v in (search or {}).items()}
        params.setdefault("_count", int(config.get("page_size") or 50))
        max_pages = max(1, int(config.get("max_pages") or 5))
        timeout = float(config.get("timeout") or 30)

        headers = {"Accept": "application/fhir+json"}
        await _auth(config, timeout=timeout, query=params, headers=headers)

        url: str | None = f"{base}/{resource}"
        resources: list[Any] = []
        pages = 0
        while url and pages < max_pages:
            response = await _http_request(
                url, headers=headers, params=params if pages == 0 else None, timeout=timeout
            )
            pages += 1
            if response.get("error"):
                message = f"FHIR query failed: {response.get('detail') or response['error']}"
                await ctx.log(message, "error")
                if config.get("fail_on_error", True):
                    raise ValueError(message)
                return {"out": {"error": response["error"]}, "text": message, "count": 0}
            if not response.get("ok"):
                message = f"FHIR server returned HTTP {response.get('status')}: {str(response.get('body'))[:300]}"
                await ctx.log(message, "error")
                if config.get("fail_on_error", True):
                    raise ValueError(message)
                return {"out": {"error": "http_error", "status": response.get("status")}, "text": message, "count": 0}

            body = response.get("body")
            if isinstance(body, dict) and body.get("resourceType") == "Bundle":
                for entry in body.get("entry") or []:
                    if isinstance(entry, dict) and isinstance(entry.get("resource"), dict):
                        resources.append(entry["resource"])
                url = _next_link(body)
            else:
                resources.append(body)
                url = None

        text = json.dumps(resources, ensure_ascii=False)
        await ctx.log(f"FHIR {resource}: {len(resources)} resource(s) in {pages} page(s).", "info")
        return {"out": resources, "text": text, "count": len(resources)}


def _next_link(bundle: dict[str, Any]) -> str | None:
    for link in bundle.get("link") or []:
        if isinstance(link, dict) and link.get("relation") == "next" and link.get("url"):
            return str(link["url"])
    return None


# ---------------------------------------------------------------------------
# SQL (read-only)
# ---------------------------------------------------------------------------


SQL_SPEC = NodeTypeSpec(
    type="sql_query",
    label="SQL query",
    label_zh="SQL 查询",
    category=NodeCategory.INTEGRATION,
    description="Run a read-only SELECT against a business database.",
    description_zh="对业务数据库执行只读 SELECT 查询。",
    icon="database",
    color="#22c55e",
    inputs=[
        InputPort(
            id="in",
            label="Context",
            label_zh="上下文",
            type=PortType.ANY,
            description="Supplies template variables for the query and parameters.",
            description_zh="为查询语句和参数提供模板变量。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Rows", label_zh="结果行", type=PortType.JSON),
        OutputPort(id="text", label="Text", label_zh="文本", type=PortType.TEXT),
        OutputPort(id="count", label="Row count", label_zh="行数", type=PortType.NUMBER),
    ],
    params=[
        SelectParam(
            name="driver",
            label="Driver",
            label_zh="驱动",
            default="sqlite",
            options=[
                SelectOption(label="SQLite", value="sqlite"),
                SelectOption(label="MySQL", value="mysql"),
                SelectOption(label="PostgreSQL", value="postgres"),
            ],
        ),
        StringParam(
            name="sqlite_path",
            label="SQLite file",
            label_zh="SQLite 文件",
            description="Absolute path to a .db file. Opened read-only.",
            description_zh=".db 文件的绝对路径，以只读方式打开。",
        ),
        StringParam(name="host", label="Host", label_zh="主机", default="127.0.0.1"),
        NumberParam(name="port", label="Port", label_zh="端口", default=3306, integer=True),
        StringParam(name="database", label="Database", label_zh="数据库"),
        StringParam(name="username", label="Username", label_zh="用户名"),
        StringParam(
            name="password",
            label="Password",
            label_zh="密码",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        StringParam(
            name="query",
            label="SQL (SELECT only)",
            label_zh="SQL（仅 SELECT）",
            description="A single read-only statement. Placeholders: ? for SQLite, %s for MySQL/PostgreSQL.",
            description_zh="单条只读语句。占位符：SQLite 用 ?，MySQL/PostgreSQL 用 %s。",
            multiline=True,
            rows=8,
            required=True,
        ),
        JsonParam(
            name="params",
            label="Positional parameters",
            label_zh="位置参数",
            description='JSON array bound to the query placeholders, e.g. ["2026-01-01"].',
            description_zh='绑定到 SQL 占位符的 JSON 数组，例如 ["2026-01-01"]。',
            default=[],
            rows=4,
        ),
        NumberParam(
            name="max_rows",
            label="Max rows",
            label_zh="最多行数",
            default=200,
            min=1,
            max=5000,
        ),
        NumberParam(
            name="timeout",
            label="Timeout (seconds)",
            label_zh="超时（秒）",
            default=30,
            min=1,
            max=300,
        ),
        BooleanParam(
            name="fail_on_error",
            label="Fail the run on error",
            label_zh="出错时让运行失败",
            default=True,
        ),
    ],
    ai_notes="""\
Use this for a customer database that is not the workflow's own results store.
For the results database written by `db_write`, use the `db_query` tool instead —
it needs no connection details.

Read-only by design: only a single SELECT/WITH/SHOW/DESCRIBE/EXPLAIN statement is
accepted. DML/DDL (INSERT, UPDATE, DELETE, DROP, ...) is refused before the
driver sees it, and SQLite is opened with `mode=ro`. This node cannot be used to
change a production database; a business action that writes belongs behind an
approved API or a human, not here.

`out` is an array of objects (column name → value). `count` is the number of
returned rows, capped by `max_rows`; it is not a COUNT(*) of the whole table.
Use query parameters rather than string interpolation for values that come from
the graph. The connection password may be `env:NAME`.

Drivers: SQLite and MySQL (pymysql) are bundled; PostgreSQL works only when
`psycopg2` is installed in the deployment. A missing driver fails with a clear
message rather than silently returning nothing.\
""",
)


_READ_ONLY_START = re.compile(r"^\s*(select|with|show|describe|explain)\b", re.I)
_WRITE_WORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|call|merge|replace|attach|pragma|into|outfile|dumpfile)\b",
    re.I,
)


def _check_read_only(query: str) -> str:
    statement = (query or "").strip()
    if not statement:
        raise ValueError("SQL query is empty.")
    trimmed = statement.rstrip().rstrip(";").strip()
    if ";" in trimmed:
        raise ValueError("Only one SQL statement is allowed.")
    if not _READ_ONLY_START.match(trimmed):
        raise ValueError("Only read-only statements (SELECT/WITH/...) are allowed.")
    match = _WRITE_WORDS.search(trimmed)
    if match:
        raise ValueError(
            f"Write keyword '{match.group(0)}' is not allowed; this node is read-only."
        )
    return trimmed


def _run_sql(config: dict[str, Any]) -> list[dict[str, Any]]:
    driver = str(config.get("driver") or "sqlite")
    query = _check_read_only(str(config.get("query") or ""))
    params = list(config.get("params") or [])
    max_rows = max(1, int(config.get("max_rows") or 200))
    timeout = max(1, int(config.get("timeout") or 30))

    if driver == "sqlite":
        path = str(config.get("sqlite_path") or "").strip()
        if not path:
            raise ValueError("SQLite query needs a file path.")
        if not Path(path).is_file():
            raise ValueError(f"SQLite file does not exist: {path}")
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=timeout)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(query, params)
            rows = [dict(row) for row in cursor.fetchmany(max_rows)]
        finally:
            conn.close()
        return rows

    if driver == "mysql":
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - deployment dependent
            raise ValueError("MySQL support needs the 'pymysql' package.") from exc
        conn = pymysql.connect(
            host=str(config.get("host") or "127.0.0.1"),
            port=int(config.get("port") or 3306),
            user=str(config.get("username") or ""),
            password=_secret(config.get("password")),
            database=str(config.get("database") or "") or None,
            connect_timeout=timeout,
            read_timeout=timeout,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            with conn.cursor() as cursor:
                cursor.execute(query, tuple(params))
                rows = list(cursor.fetchmany(max_rows))
        finally:
            conn.close()
        return rows

    if driver == "postgres":
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:  # pragma: no cover - deployment dependent
            raise ValueError(
                "PostgreSQL support needs 'psycopg2-binary' installed in the deployment."
            ) from exc
        conn = psycopg2.connect(
            host=str(config.get("host") or "127.0.0.1"),
            port=int(config.get("port") or 5432),
            user=str(config.get("username") or ""),
            password=_secret(config.get("password")),
            dbname=str(config.get("database") or ""),
            connect_timeout=timeout,
        )
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(query, tuple(params))
                rows = [dict(row) for row in cursor.fetchmany(max_rows)]
        finally:
            conn.close()
        return rows

    raise ValueError(f"Unknown SQL driver '{driver}'.")


@register_node
class SqlQueryNode(BaseNode):
    spec = SQL_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        _guard_public(ctx, "SQL query")
        config = self.resolve_config(ctx)
        scope = template_scope(ctx, config)
        rendered = dict(config)
        rendered["query"] = _fill(config.get("query") or "", scope)
        rendered["params"] = _fill(config.get("params") or [], scope)
        try:
            rows = await asyncio.to_thread(_run_sql, rendered)
        except Exception as exc:
            message = f"SQL query failed: {exc}"
            await ctx.log(message, "error")
            if config.get("fail_on_error", True):
                raise ValueError(message) from exc
            return {"out": {"error": str(exc)}, "text": message, "count": 0}

        rows = _jsonable(rows)
        text = json.dumps(rows, ensure_ascii=False)
        await ctx.log(f"SQL query returned {len(rows)} row(s).", "info")
        return {"out": rows, "text": text, "count": len(rows)}


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


EMAIL_SPEC = NodeTypeSpec(
    type="email_send",
    label="Email",
    label_zh="发送邮件",
    category=NodeCategory.INTEGRATION,
    description="Send a plain-text or HTML email through an SMTP relay.",
    description_zh="通过 SMTP 中继发送纯文本或 HTML 邮件。",
    icon="mail",
    color="#eab308",
    inputs=[
        InputPort(
            id="in",
            label="Context",
            label_zh="上下文",
            type=PortType.ANY,
            description="Supplies template variables for recipients and body.",
            description_zh="为收件人和正文提供模板变量。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Result", label_zh="结果", type=PortType.JSON),
        OutputPort(id="text", label="Text", label_zh="文本", type=PortType.TEXT),
    ],
    params=[
        StringParam(name="smtp_host", label="SMTP host", label_zh="SMTP 主机", required=True),
        NumberParam(name="smtp_port", label="SMTP port", label_zh="SMTP 端口", default=587, integer=True),
        SelectParam(
            name="security",
            label="Security",
            label_zh="加密方式",
            default="starttls",
            options=[
                SelectOption(label="STARTTLS", value="starttls"),
                SelectOption(label="SSL/TLS", value="ssl"),
                SelectOption(label="None (internal relay)", label_zh="无（内网中继）", value="none"),
            ],
        ),
        StringParam(name="username", label="Username", label_zh="用户名"),
        StringParam(
            name="password",
            label="Password",
            label_zh="密码",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        StringParam(name="from_addr", label="From", label_zh="发件人", required=True),
        StringParam(
            name="to",
            label="To",
            label_zh="收件人",
            description="Comma-separated addresses; {{ }} allowed.",
            description_zh="逗号分隔的地址，可用 {{ }}。",
            required=True,
        ),
        StringParam(name="cc", label="Cc", label_zh="抄送"),
        StringParam(
            name="subject",
            label="Subject",
            label_zh="主题",
            description="{{ }} allowed. Do not put patient identifiers here.",
            description_zh="可用 {{ }}。不要在主题里放患者身份信息。",
            default="",
        ),
        StringParam(
            name="body",
            label="Body",
            label_zh="正文",
            multiline=True,
            rows=8,
            default="",
        ),
        BooleanParam(name="html", label="HTML body", label_zh="HTML 正文", default=False),
        NumberParam(name="timeout", label="Timeout (seconds)", label_zh="超时（秒）", default=30, min=1, max=300),
        BooleanParam(
            name="fail_on_error",
            label="Fail the run on error",
            label_zh="出错时让运行失败",
            default=True,
        ),
    ],
    ai_notes="""\
Use this for notifications and scheduled reports when the deployment has an SMTP
relay. For an HTTP webhook (Slack, Teams, Feishu, WeCom) use the `http_request`
tool instead; this node is for real email.

Recipients, subject and body are templates, so `{{ vars.report.out }}` works.
Keep identifiers out of the subject line; the body is not encrypted in transit
beyond the SMTP hop, and mail servers keep copies. For hospital use, point this
at the internal relay and do not send PHI to external addresses.

`out` is `{"sent": true, "recipients": N}`. Set `fail_on_error: false` when a
notification failure should not fail the business run. Credentials may be
`env:NAME`.\
""",
)


def _parse_addresses(value: str) -> list[str]:
    return [part.strip() for part in re.split(r"[,;]", value or "") if part.strip()]


def _send_mail(config: dict[str, Any]) -> int:
    recipients = _parse_addresses(str(config.get("to") or ""))
    cc = _parse_addresses(str(config.get("cc") or ""))
    if not recipients:
        raise ValueError("Email has no recipients.")
    sender = str(config.get("from_addr") or "").strip()
    if not sender:
        raise ValueError("Email has no From address.")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    if cc:
        message["Cc"] = ", ".join(cc)
    message["Subject"] = str(config.get("subject") or "")
    body = str(config.get("body") or "")
    if config.get("html"):
        message.set_content("This message requires an HTML-capable mail client.")
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)

    host = str(config.get("smtp_host") or "")
    port = int(config.get("smtp_port") or 587)
    timeout = float(config.get("timeout") or 30)
    security = str(config.get("security") or "starttls")
    if security == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout)
    else:
        client = smtplib.SMTP(host, port, timeout=timeout)
    try:
        if security == "starttls":
            client.starttls()
        username = str(config.get("username") or "")
        password = _secret(config.get("password"))
        if username:
            client.login(username, password)
        client.send_message(message, to_addrs=recipients + cc)
    finally:
        try:
            client.quit()
        except Exception:  # noqa: BLE001 - best-effort close
            pass
    return len(recipients) + len(cc)


@register_node
class EmailSendNode(BaseNode):
    spec = EMAIL_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        _guard_public(ctx, "Email")
        config = self.resolve_config(ctx)
        scope = template_scope(ctx, config)
        rendered = {key: _fill(value, scope) if isinstance(value, str) else value for key, value in config.items()}
        try:
            sent = await asyncio.to_thread(_send_mail, rendered)
        except Exception as exc:
            message = f"Email failed: {exc}"
            await ctx.log(message, "error")
            if config.get("fail_on_error", True):
                raise ValueError(message) from exc
            return {"out": {"sent": False, "error": str(exc)}, "text": message}
        await ctx.log(f"Email sent to {sent} recipient(s).", "info")
        return {"out": {"sent": True, "recipients": sent}, "text": f"Sent to {sent} recipient(s)."}


# ---------------------------------------------------------------------------
# Document text extraction
# ---------------------------------------------------------------------------


DOCUMENT_SPEC = NodeTypeSpec(
    type="document_extract",
    label="Document text",
    label_zh="文档抽取",
    category=NodeCategory.INTEGRATION,
    description="Extract text from a PDF (URL or file under DATA_DIR).",
    description_zh="从 PDF 中提取文字（URL 或 DATA_DIR 下的文件）。",
    icon="file-search",
    color="#8b5cf6",
    inputs=[
        InputPort(
            id="in",
            label="Context",
            label_zh="上下文",
            type=PortType.ANY,
            description="Supplies template variables for the URL or path.",
            description_zh="为 URL 或路径提供模板变量。",
        )
    ],
    outputs=[
        OutputPort(id="out", label="Text", label_zh="文本", type=PortType.TEXT),
        OutputPort(id="meta", label="Metadata", label_zh="元数据", type=PortType.JSON),
        OutputPort(id="pages", label="Pages", label_zh="页数", type=PortType.NUMBER),
    ],
    params=[
        SelectParam(
            name="source",
            label="Source",
            label_zh="来源",
            default="url",
            options=[
                SelectOption(label="URL", value="url"),
                SelectOption(label="File under DATA_DIR", label_zh="DATA_DIR 下的文件", value="file"),
            ],
        ),
        StringParam(
            name="url",
            label="PDF URL",
            label_zh="PDF 地址",
            description="Fetched through the egress policy.",
            description_zh="通过出网策略抓取。",
        ),
        StringParam(
            name="file_path",
            label="File path",
            label_zh="文件路径",
            description="Relative to DATA_DIR, or an absolute path inside it.",
            description_zh="相对 DATA_DIR，或 DATA_DIR 内的绝对路径。",
        ),
        NumberParam(
            name="max_pages",
            label="Max pages",
            label_zh="最多页数",
            default=20,
            min=1,
            max=200,
        ),
        NumberParam(
            name="max_chars",
            label="Max characters",
            label_zh="最多字符",
            default=20000,
            min=100,
            max=200000,
        ),
        StringParam(
            name="password",
            label="PDF password",
            label_zh="PDF 密码",
            description="Use env:NAME to read it from the environment.",
            description_zh="可用 env:NAME 从环境变量读取。",
            secret=True,
        ),
        BooleanParam(
            name="fail_on_error",
            label="Fail the run on error",
            label_zh="出错时让运行失败",
            default=True,
        ),
    ],
    ai_notes="""\
Use this when the business input is a PDF: contracts, referral letters,
discharge summaries, invoices, lab reports. It extracts **embedded text only**.
A scanned/image-only PDF returns little or nothing — OCR is not configured in
this deployment, so do not claim the text was read when `pages` is 0 or the text
is empty; route it to a human instead.

`source: file` is deliberately restricted to DATA_DIR so the node cannot read
arbitrary files from the host. Put uploaded documents there (or pass a URL).
`out` is the concatenated text, `meta` is the PDF metadata, `pages` is the
number of pages actually read (capped by `max_pages`). Truncation is reported in
`meta.truncated`.

This node does not write, sign, or alter the document.\
""",
)


async def _fetch_binary(url: str, *, timeout: float) -> bytes:
    policy = get_egress_policy()
    decision = policy.check(url)
    if not decision.allowed:
        raise ValueError(f"Document URL refused by egress policy: {decision.reason}")
    cap = max(1, int(get_settings_cap()))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.get(url)
        if response.is_redirect and response.headers.get("location"):
            target = str(httpx.URL(url).join(response.headers["location"]))
            decision = policy.check(target)
            if not decision.allowed:
                raise ValueError(f"Redirect refused by egress policy: {decision.reason}")
            response = await client.get(target)
        if not response.is_success:
            raise ValueError(f"Document URL returned HTTP {response.status_code}.")
        data = response.content
        if len(data) > cap:
            raise ValueError(f"Document exceeds the {cap}-byte response cap.")
        return data


def get_settings_cap() -> int:
    from app.settings import get_settings

    return int(get_settings().egress_max_response_bytes)


def _allowed_file(path: str, ctx: NodeContext) -> Path:
    root = Path(ctx.settings.data_dir).resolve()
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("Document path must be inside DATA_DIR.")
    if not resolved.is_file():
        raise ValueError(f"Document does not exist: {path}")
    return resolved


def _extract_pdf(data: bytes, *, max_pages: int, password: str) -> tuple[str, int, dict[str, Any]]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - deployment dependent
        raise ValueError("PDF support needs the 'pypdf' package.") from exc

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        if not password or not reader.decrypt(password):
            raise ValueError("PDF is encrypted and the password is missing or wrong.")
    pages = list(reader.pages[:max_pages])
    parts: list[str] = []
    for page in pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001 - a damaged page must not kill the document
            parts.append("")
    text = "\n\n".join(part.strip() for part in parts if part.strip())
    metadata = {}
    try:
        metadata = {str(k): _jsonable(v) for k, v in (reader.metadata or {}).items()}
    except Exception:  # noqa: BLE001
        metadata = {}
    return text, len(pages), metadata


@register_node
class DocumentExtractNode(BaseNode):
    spec = DOCUMENT_SPEC

    async def run(self, ctx: NodeContext) -> dict[str, Any]:
        _guard_public(ctx, "Document text")
        config = self.resolve_config(ctx)
        scope = template_scope(ctx, config)
        source = str(config.get("source") or "url")
        timeout = 30.0
        try:
            if source == "file":
                path = _allowed_file(str(_fill(config.get("file_path") or "", scope)), ctx)
                data = path.read_bytes()
            else:
                url = str(_fill(config.get("url") or "", scope)).strip()
                if not url:
                    raise ValueError("Document text has no URL.")
                data = await _fetch_binary(url, timeout=timeout)
            text, pages, metadata = await asyncio.to_thread(
                _extract_pdf,
                data,
                max_pages=max(1, int(config.get("max_pages") or 20)),
                password=_secret(config.get("password")),
            )
        except Exception as exc:
            message = f"Document extraction failed: {exc}"
            await ctx.log(message, "error")
            if config.get("fail_on_error", True):
                raise ValueError(message) from exc
            return {"out": f"ERROR: {message}", "meta": {"error": str(exc)}, "pages": 0}

        limit = max(100, int(config.get("max_chars") or 20000))
        truncated = len(text) > limit
        if truncated:
            text = text[:limit]
            metadata["truncated"] = True
        await ctx.log(f"Extracted {pages} page(s), {len(text)} character(s).", "info")
        return {"out": text, "meta": metadata, "pages": pages}


__all__ = [
    "PUBLIC_DEMO_DISABLED_TYPES",
    "ApiCallNode",
    "DocumentExtractNode",
    "EmailSendNode",
    "FhirQueryNode",
    "SqlQueryNode",
]
