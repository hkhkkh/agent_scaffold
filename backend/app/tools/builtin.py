"""Built-in tools.

Deliberately small and dependency-light. Each one is a thin wrapper whose value
comes from being *discoverable* — the Agent node can call any registered tool
without the workflow author writing plumbing.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import urllib.parse
from typing import Any

import httpx

from app.runtime.egress import get_egress_policy
from app.runtime.expr import ExpressionError, evaluate
from app.settings import get_settings
from app.tools.registry import ToolDef, register_tool


# ---------------------------------------------------------------------------
# http_request
# ---------------------------------------------------------------------------
def _read_capped(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
    """Read at most ``cap`` bytes. Returns ``(body, truncated)``.

    ``response.read()`` would pull the whole thing into memory before any size
    check could run, which is the wrong order — a hostile or merely huge endpoint
    gets to decide how much RAM the process uses. Streaming with a running total
    is the only version where the cap is real.
    """
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= cap:
            return b"".join(chunks)[:cap], True
    return b"".join(chunks), False


async def _http_request(
    url: str,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    body: Any = None,
    timeout: float = 30.0,
    form: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call an HTTP endpoint, subject to the process egress policy.

    Redirects are followed by hand rather than by httpx. ``follow_redirects=True``
    would re-request whatever ``Location`` says with no second opinion, which is
    the classic way past a URL check: point the tool at a public host that
    answers ``302 -> http://169.254.169.254/``. Each hop is re-checked here.
    """
    settings = get_settings()
    policy = get_egress_policy()

    decision = policy.check(url)
    if not decision.allowed:
        return {
            "error": "egress_denied",
            "detail": decision.reason,
            "url": url,
            "hint": (
                "Outbound requests are filtered. Set EGRESS_ALLOWED_HOSTS to "
                "permit this host, or EGRESS_ALLOW_PRIVATE=true to permit the "
                "whole private range (not recommended)."
            ),
        }

    verb = (method or "GET").upper()
    max_redirects = max(0, int(settings.egress_max_redirects))
    cap = max(1, int(settings.egress_max_response_bytes))

    current = url
    hops: list[str] = []

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for _hop in range(max_redirects + 1):
            response = await client.request(
                verb,
                current,
                headers=headers or {},
                # `or None`, never `or {}`. Handing httpx an empty params dict
                # makes it rewrite the URL's query string, and Bing answers the
                # rewritten form with a 302 carrying no results — measured:
                # 302/128B with `params={}`, 200/101KB with the query untouched.
                # Every caller that passed no params was sending `{}`.
                params=params or None,
                json=body if verb in {"POST", "PUT", "PATCH"} and form is None else None,
                # OAuth2 token endpoints require form encoding. Added for the
                # API/FHIR nodes; the `http_request` tool never passes it.
                data=form if form is not None else None,
            )

            location = response.headers.get("location")
            if response.is_redirect and location:
                # Resolve relative to the URL that produced it, then treat the
                # result exactly like a user-supplied URL.
                nxt = str(httpx.URL(current).join(location))
                next_decision = policy.check(nxt)
                if not next_decision.allowed:
                    await response.aclose()
                    return {
                        "error": "egress_denied",
                        "detail": f"redirect to a blocked destination: {next_decision.reason}",
                        "url": url,
                        "redirect_to": nxt,
                        "hops": hops,
                    }
                hops.append(current)
                current = nxt
                # Read before closing. httpx stores a response's cookies when it
                # is read, so closing first throws away the `Set-Cookie` that
                # arrived with the redirect — and the next hop then looks like a
                # first-time visitor. Measured on Bing: 15KB of results-free
                # shell without that cookie, 104KB of real results with it.
                await response.aread()
                await response.aclose()
                continue

            raw, truncated = _read_capped(response, cap)
            text = raw.decode(response.encoding or "utf-8", errors="replace")
            parsed: Any = None
            if "json" in response.headers.get("content-type", ""):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
            result: dict[str, Any] = {
                "status": response.status_code,
                "ok": response.is_success,
                # A JSON body used to be returned whole with no cap at all. The
                # cap now applies to every content type.
                "body": parsed if parsed is not None else text,
                "content_type": response.headers.get("content-type", ""),
            }
            if truncated:
                result["truncated"] = True
                result["detail"] = (
                    f"Response exceeded {cap} bytes and was cut off. "
                    "Raise EGRESS_MAX_RESPONSE_BYTES to see more."
                )
            if hops:
                result["redirects"] = hops
            return result

    return {
        "error": "too_many_redirects",
        "detail": f"More than {max_redirects} redirect(s) from {url}.",
        "url": url,
        "hops": hops,
    }


register_tool(
    ToolDef(
        name="http_request",
        label="HTTP Request",
        label_zh="HTTP 请求",
        description=(
            "Call an HTTP endpoint. Use this to fetch a web page or query a REST API."
        ),
        description_zh="调用一个 HTTP 接口，用来抓取网页或查询 REST API。",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Absolute URL to call."},
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                },
                "headers": {"type": "object", "description": "Optional request headers."},
                "params": {"type": "object", "description": "Optional query parameters."},
                "body": {"type": "object", "description": "JSON body for POST/PUT/PATCH."},
            },
            "required": ["url"],
        },
        fn=_http_request,
        dangerous=True,
    )
)


# ---------------------------------------------------------------------------
# calculator
# ---------------------------------------------------------------------------
def _calculator(expression: str) -> dict[str, Any]:
    try:
        value = evaluate(expression, {})
    except ExpressionError as exc:
        return {"error": str(exc), "expression": expression}
    return {"expression": expression, "result": value}


register_tool(
    ToolDef(
        name="calculator",
        label="Calculator",
        label_zh="计算器",
        description=(
            "Evaluate an arithmetic expression. Supports +, -, *, /, //, %, **, "
            "comparisons, and functions such as abs, min, max, round, sum, sorted."
        ),
        description_zh=(
            "计算一个算术表达式。支持 + - * / // % ** 和比较运算，"
            "以及 abs、min、max、round、sum、sorted 等函数。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "e.g. '(12 * 7) + 3' or 'sum([1,2,3])'",
                }
            },
            "required": ["expression"],
        },
        fn=_calculator,
    )
)


# ---------------------------------------------------------------------------
# current_time
# ---------------------------------------------------------------------------
def _current_time(timezone_offset_hours: float = 0) -> dict[str, Any]:
    tz = dt.timezone(dt.timedelta(hours=timezone_offset_hours))
    now = dt.datetime.now(tz)
    return {
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "weekday": now.strftime("%A"),
        "timezone_offset_hours": timezone_offset_hours,
    }


register_tool(
    ToolDef(
        name="current_time",
        label="Current Time",
        label_zh="当前时间",
        description="Get the current date and time. Use when the answer depends on 'now'.",
        description_zh="获取当前日期和时间。当答案和「现在」有关时使用。",
        parameters={
            "type": "object",
            "properties": {
                "timezone_offset_hours": {
                    "type": "number",
                    "description": "UTC offset, e.g. 8 for UTC+8 (China). Default 0.",
                    "default": 0,
                }
            },
        },
        fn=_current_time,
    )
)


# ---------------------------------------------------------------------------
# json_query
# ---------------------------------------------------------------------------
def _json_query(data: Any, path: str, default: Any = None) -> dict[str, Any]:
    """Read a dotted path out of nested data.

    ``path`` accepts ``a.b.0.c`` for dicts and lists alike, which is the shape
    models reliably produce when asked to pull a field out of an API response.
    """
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return {"found": False, "value": default, "error": "Input is not JSON."}

    current = data
    for part in str(path or "").split("."):
        if not part:
            continue
        if isinstance(current, dict):
            if part not in current:
                return {"found": False, "value": default, "path": path}
            current = current[part]
        elif isinstance(current, list):
            if not part.lstrip("-").isdigit():
                return {"found": False, "value": default, "path": path}
            index = int(part)
            if index >= len(current) or index < -len(current):
                return {"found": False, "value": default, "path": path}
            current = current[index]
        else:
            return {"found": False, "value": default, "path": path}
    return {"found": True, "value": current, "path": path}


register_tool(
    ToolDef(
        name="json_query",
        label="JSON Query",
        label_zh="JSON 取值",
        description=(
            "Read a value from nested JSON using a dotted path, e.g. 'data.items.0.name'."
        ),
        description_zh=(
            "用点号路径从嵌套 JSON 里取值，例如 'data.items.0.name'。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "data": {"description": "The JSON value or JSON string to search."},
                "path": {"type": "string", "description": "Dotted path, e.g. 'a.b.0.c'."},
                "default": {"description": "Returned when the path is missing."},
            },
            "required": ["data", "path"],
        },
        fn=_json_query,
    )
)


# ---------------------------------------------------------------------------
# text_stats
# ---------------------------------------------------------------------------
def _text_stats(text: str) -> dict[str, Any]:
    words = str(text or "").split()
    return {
        "characters": len(str(text or "")),
        "words": len(words),
        "lines": len(str(text or "").splitlines()),
        "preview": str(text or "")[:200],
    }


register_tool(
    ToolDef(
        name="text_stats",
        label="Text Stats",
        label_zh="文本统计",
        description="Count characters, words, and lines; return a short preview.",
        description_zh="统计字符数、词数和行数，并返回一小段预览。",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to measure."}},
            "required": ["text"],
        },
        fn=_text_stats,
    )
)


# ---------------------------------------------------------------------------
# web_search
# ---------------------------------------------------------------------------
#
# Why this exists, stated plainly: `http_request` is not a search. An agent given
# only `http_request` has to *name* a URL, and it cannot — it invents them, gets
# 404s, and falls back to reciting from memory while appearing to have looked
# something up. (Observed: a run spent 290k tokens fetching a government SPA's
# `<script>` skeleton three times, then answered from model knowledge anyway.)
#
# These engines answer without an API key, which is what keeps the tool usable in
# a default deployment. Their HTML is scraped, so the parsers below are the part
# most likely to need maintenance; each one fails soft and the caller names the
# engine it actually used.

#: A browser user agent. Both engines serve a bare client a consent or bot page,
#: and getting past that without a key is the whole point of the tool.
_SEARCH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: Headers that make an engine answer with a real result page.
#:
#: `Accept-Language` is load-bearing here, not decoration. Without it Bing serves
#: a results-free shell — measured at 15KB with no `b_algo` blocks, against 100KB
#: with them once the header is present. The parser then finds nothing, and the
#: tool reports "no results", which is true but blames the wrong party.
_SEARCH_HEADERS = {
    "User-Agent": _SEARCH_UA,
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

#: Cap on one call. Results go into a model's context, where more is not better.
MAX_SEARCH_RESULTS = 10


def _plain(fragment: str) -> str:
    """A result fragment as readable text."""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _unwrap_duckduckgo(href: str) -> str:
    """DuckDuckGo returns a redirect; the real target rides in ``uddg``.

    Handing the model a ``duckduckgo.com/l/?uddg=...`` link would be a citation
    it cannot use and a second hop for anything that follows it.
    """
    if "uddg=" not in href:
        return href
    query = urllib.parse.urlparse(href).query
    target = urllib.parse.parse_qs(query).get("uddg", [""])[0]
    return target or href


def _parse_duckduckgo(page: str, limit: int) -> list[dict[str, str]]:
    """DuckDuckGo's no-JavaScript page.

    The simplest of the keyless engines: one anchor per result and one snippet
    anchor after it, in the same order, so position pairs them.
    """
    anchors = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S
    )
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    return [
        {
            "title": _plain(raw_title),
            "url": _unwrap_duckduckgo(href),
            "snippet": _plain(snippets[index]) if index < len(snippets) else "",
        }
        for index, (href, raw_title) in enumerate(anchors[:limit])
    ]


def _parse_bing(page: str, limit: int) -> list[dict[str, str]]:
    """Bing's result list.

    Rougher than the above on purpose: Bing's markup carries inline stylesheets
    and extra wrappers, so this matches the shape rather than the exact bytes and
    skips any block it cannot read.
    """
    blocks = re.findall(
        r'<li class="b_algo".*?(?=<li class="b_algo"|</ol>)', page, re.S
    )
    results: list[dict[str, str]] = []
    for block in blocks:
        if len(results) >= limit:
            break
        link = re.search(
            r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S
        )
        if not link:
            continue
        paragraph = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        results.append(
            {
                "title": _plain(link.group(2)),
                "url": link.group(1),
                "snippet": _plain(paragraph.group(1)) if paragraph else "",
            }
        )
    return results


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[㐀-鿿]", text))


def _parse_baidu(page: str, limit: int) -> list[dict[str, str]]:
    """Baidu's result list.

    Baidu rewrites result links through ``www.baidu.com/link?url=...``; keeping
    that hop is preferable to inventing a target, and the snippet is still the
    useful part for the model. The page is large, so each h3 is paired with the
    text up to the next h3 rather than walking the whole DOM.
    """
    results: list[dict[str, str]] = []
    positions = list(re.finditer(r"<h3[^>]*>(.*?)</h3>", page, re.S | re.I))
    for index, match in enumerate(positions):
        if len(results) >= limit:
            break
        block = match.group(1)
        link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S | re.I)
        if not link:
            continue
        title = _plain(link.group(2))
        if not title:
            title = _plain(block)
        tail_end = positions[index + 1].start() if index + 1 < len(positions) else len(page)
        tail = page[match.end() : tail_end]
        snippet = re.search(
            r'<(?:p|div)[^>]*class="[^"]*(?:c-abstract|content-right|cos-text-body|c-line-clamp)[^"]*"[^>]*>(.*?)</(?:p|div)>',
            tail,
            re.S | re.I,
        )
        results.append(
            {
                "title": title,
                "url": html.unescape(link.group(1)),
                "snippet": _plain(snippet.group(1))[:400] if snippet else "",
            }
        )
    return results


def _parse_so(page: str, limit: int) -> list[dict[str, str]]:
    """360 Search (so.com). ``data-mdurl`` is the real publisher URL."""
    results: list[dict[str, str]] = []
    blocks = re.findall(
        r'<h3[^>]*class="[^"]*res-title[^"]*"[^>]*>(.*?)</h3>(.*?)(?=<h3[^>]*class="[^"]*res-title|$)',
        page,
        re.S | re.I,
    )
    for heading, tail in blocks[:limit]:
        link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', heading, re.S | re.I)
        if not link:
            continue
        real = re.search(r'data-mdurl="([^"]+)"', heading, re.S | re.I)
        snippet = re.search(
            r'<(?:p|span)[^>]*class="[^"]*(?:res-desc|res-list-summary)[^"]*"[^>]*>(.*?)</(?:p|span)>',
            tail,
            re.S | re.I,
        )
        results.append(
            {
                "title": _plain(link.group(2)),
                "url": html.unescape(real.group(1)) if real else html.unescape(link.group(1)),
                "snippet": _plain(snippet.group(1))[:400] if snippet else "",
            }
        )
    return results


def _parse_sogou(page: str, limit: int) -> list[dict[str, str]]:
    """Sogou's web result list."""
    results: list[dict[str, str]] = []
    blocks = re.findall(
        r'<h3[^>]*class="[^"]*vr-title[^"]*"[^>]*>(.*?)</h3>(.*?)(?=<h3[^>]*class="[^"]*vr-title|$)',
        page,
        re.S | re.I,
    )
    for heading, tail in blocks[:limit]:
        link = re.search(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', heading, re.S | re.I)
        if not link:
            continue
        snippet = re.search(r"<p[^>]*>(.*?)</p>", tail, re.S | re.I)
        results.append(
            {
                "title": _plain(link.group(2)),
                "url": html.unescape(link.group(1)),
                "snippet": _plain(snippet.group(1))[:400] if snippet else "",
            }
        )
    return results


#: Engines for Latin queries. DuckDuckGo first because it returns clean snippets;
#: Bing is the fallback and is named in the reply when it answers.
_ENGINES: tuple[tuple[str, str, Any], ...] = (
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={query}", _parse_duckduckgo),
    ("bing", "https://cn.bing.com/search?q={query}", _parse_bing),
)

#: Engines for CJK queries. DuckDuckGo's HTML endpoint answers Chinese queries
#: with a 202 anomaly page and Bing's Chinese ranking is unstable; the domestic
#: engines are the reliable path, with the global pair as a last resort.
_CN_ENGINES: tuple[tuple[str, str, Any], ...] = (
    ("baidu", "https://www.baidu.com/s?wd={query}", _parse_baidu),
    ("sogou", "https://www.sogou.com/web?query={query}", _parse_sogou),
    ("so", "https://www.so.com/s?q={query}", _parse_so),
    ("duckduckgo", "https://html.duckduckgo.com/html/?q={query}", _parse_duckduckgo),
    ("bing", "https://cn.bing.com/search?q={query}", _parse_bing),
)


async def _web_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search the web and return ranked results.

    Goes through :func:`_http_request` rather than opening its own client, so it
    inherits the egress policy, the manual redirect re-checks, and the response
    size cap. One network layer in this module, not two.
    """
    text = str(query or "").strip()
    if not text:
        return {"error": "empty_query", "detail": "A search query is required."}

    limit = max(1, min(int(max_results or 5), MAX_SEARCH_RESULTS))
    encoded = urllib.parse.quote(text)

    attempts: list[str] = []
    engines = _CN_ENGINES if _has_cjk(text) else _ENGINES
    for engine, template, parser in engines:
        try:
            page = await _http_request(
                template.format(query=encoded),
                headers=_SEARCH_HEADERS,
                timeout=20.0,
            )
        except Exception as exc:  # noqa: BLE001
            # `_http_request` lets transport failures out, and httpx's
            # `ConnectError` stringifies to nothing — so a real failure reached
            # the agent's log as `web_search failed - ` with no reason at all.
            # Name the exception type so the reason survives even when the
            # message does not.
            reason = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
            attempts.append(f"{engine}: {reason}")
            continue

        if not page.get("ok"):
            attempts.append(f"{engine}: {page.get('error') or page.get('status')}")
            continue

        status = page.get("status")
        if status != 200:
            # 202 is a throttle, not a page: DuckDuckGo answers it with a small
            # placeholder when a host has been asking too often. Naming that
            # beats "no results parsed", which reads like a parser bug and sends
            # the next person looking in the wrong file.
            throttle = " (rate limited)" if status == 202 else ""
            attempts.append(f"{engine}: HTTP {status}{throttle}")
            continue

        body = page.get("body")
        if not isinstance(body, str):
            attempts.append(f"{engine}: non-text response")
            continue

        results = parser(body, limit)
        if not results:
            # The page loaded but the markup was not what the parser expects.
            # Falling through to the next engine beats returning an empty list
            # that reads like "the web has nothing to say about this".
            attempts.append(f"{engine}: no results parsed")
            continue

        reply: dict[str, Any] = {"query": text, "engine": engine, "results": results}
        if engine != engines[0][0]:
            reply["degraded"] = True
            reply["detail"] = (
                f"The primary engine did not answer; these results come from {engine}."
            )
        return reply

    return {
        "error": "search_failed",
        "detail": "No search engine returned usable results.",
        "attempts": attempts,
        "hint": (
            "Check outbound network access. The egress policy may be blocking "
            "these hosts, or they may be unreachable from this machine."
        ),
    }


register_tool(
    ToolDef(
        name="web_search",
        label="Web Search",
        label_zh="联网搜索",
        description=(
            "Search the web and get ranked results with title, URL and snippet. "
            "Use this instead of guessing a URL for http_request: it finds pages "
            "whose address you do not know, and it is how you verify facts you are "
            "unsure of. Cite the URLs it returns."
        ),
        description_zh=(
            "联网搜索，返回带标题、链接和摘要的排序结果。"
            "用它而不是拿 http_request 去猜网址——它能找到你说不出地址的页面，"
            "也是核实不确定事实的办法。引用时请给出返回的链接。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for."},
                "max_results": {
                    "type": "integer",
                    "description": (
                        f"How many results to return (1-{MAX_SEARCH_RESULTS})."
                    ),
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        fn=_web_search,
        dangerous=True,
    )
)


BUILTIN_TOOLS = [
    "http_request",
    "calculator",
    "current_time",
    "json_query",
    "text_stats",
    "web_search",
]


__all__ = ["BUILTIN_TOOLS"]
