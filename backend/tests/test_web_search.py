"""The ``web_search`` tool: parsing, fallback, and the ways it fails.

What is worth defending here is not "the regex matches". It is three things:

* **A search result survives the trip.** The engine's markup is scraped, so a
  result has to come out with its title, a URL the model can actually cite, and
  its snippet — in the order the engine ranked them. A silently empty list reads
  to an agent as "the web has nothing on this", which is the failure that matters.
* **The redirect is unwrapped.** DuckDuckGo hands back
  ``//duckduckgo.com/l/?uddg=<target>``; passing that on would give the model a
  citation it cannot use.
* **A weak engine is never passed off as the primary one.** Bing answers but
  ranks Chinese queries far more loosely, so a fallback reply says so.

The parsed fixtures below are the real markup shape taken from live responses,
trimmed to the parts the parsers read.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.tools import builtin
from app.tools.registry import get_tool, list_tools

DUCKDUCKGO_PAGE = """
<div class="result results_links web-result">
  <div class="links_main result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=aaa">First &amp; Foremost</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fone&amp;rut=aaa">Rolled &quot;quoted&quot; text.</a>
  </div>
</div>
<div class="result results_links web-result">
  <div class="links_main result__body">
    <h2 class="result__title">
      <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Ftwo&amp;rut=bbb">Second</a>
    </h2>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Ftwo&amp;rut=bbb">Second snippet.</a>
  </div>
</div>
"""

BING_PAGE = """
<ol id="b_results">
<li class="b_algo" data-id iid=SERP.1><link rel="stylesheet" href="/rp/x.css"/><h2><a href="https://example.com/bing-one">Bing One</a></h2><p>Bing snippet one.</p></li>
<li class="b_algo" data-id iid=SERP.2><link rel="stylesheet" href="/rp/y.css"/><h2><a href="https://example.com/bing-two">Bing Two</a></h2><p>Bing snippet two.</p></li>
</ol>
"""

BAIDU_PAGE = """
<h3 class="t"><a href="http://www.baidu.com/link?url=abc"><em>阶跃星辰</em> Agent</a></h3>
<div class="content-right">阶跃星辰发布 Agent 产品。</div>
<h3 class="t"><a href="http://www.baidu.com/link?url=def">第二条结果</a></h3>
<div class="c-abstract">第二条摘要。</div>
"""

SO_PAGE = """
<li class="res-list"><h3 class="res-title"><a href="https://www.so.com/link?m=x" data-mdurl="https://example.com/so-one"><em>阶跃星辰</em> Agent</a></h3>
<p class="res-desc">360 搜索摘要。</p></li>
<li class="res-list"><h3 class="res-title"><a href="https://www.so.com/link?m=y" data-mdurl="https://example.com/so-two">第二条</a></h3>
<p class="res-desc">第二条摘要。</p></li>
"""

SOGOU_PAGE = """
<h3 class="vr-title"><a href="https://example.com/sogou-one">阶跃星辰 Agent</a></h3>
<p>搜狗摘要。</p>
<h3 class="vr-title"><a href="https://example.com/sogou-two">第二条</a></h3>
<p>第二条摘要。</p>
"""


def _page(body: str) -> dict[str, Any]:
    return {"status": 200, "ok": True, "body": body, "content_type": "text/html"}


def _duckduckgo_page(count: int) -> str:
    """A page holding `count` results — for testing the cap, not the parser."""
    return "\n".join(
        f'<a rel="nofollow" class="result__a" '
        f'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F{i}&amp;rut=x">'
        f"Title {i}</a>"
        f'<a class="result__snippet" href="/l/?uddg=x">Snippet {i}.</a>'
        for i in range(count)
    )


# ---------------------------------------------------------------------------
# 1. Parsing
# ---------------------------------------------------------------------------


class TestParsingTheSearchPages:
    def test_duckduckgo_results_keep_the_order_the_engine_ranked_them(self):
        results = builtin._parse_duckduckgo(DUCKDUCKGO_PAGE, 5)
        assert [r["title"] for r in results] == ["First & Foremost", "Second"]
        assert [r["url"] for r in results] == [
            "https://example.com/one",
            "https://example.com/two",
        ]

    def test_the_redirect_wrapper_is_unwrapped(self):
        """The model must get a link it can cite, not a DuckDuckGo hop."""
        result = builtin._parse_duckduckgo(DUCKDUCKGO_PAGE, 1)[0]
        assert result["url"] == "https://example.com/one"
        assert "duckduckgo.com" not in result["url"]

    def test_entities_are_decoded_and_tags_stripped(self):
        result = builtin._parse_duckduckgo(DUCKDUCKGO_PAGE, 1)[0]
        assert result["snippet"] == 'Rolled "quoted" text.'

    def test_the_limit_is_respected(self):
        assert len(builtin._parse_duckduckgo(DUCKDUCKGO_PAGE, 1)) == 1
        assert len(builtin._parse_duckduckgo(DUCKDUCKGO_PAGE, 2)) == 2

    def test_bing_blocks_are_read(self):
        results = builtin._parse_bing(BING_PAGE, 5)
        assert [r["title"] for r in results] == ["Bing One", "Bing Two"]
        assert results[0]["url"] == "https://example.com/bing-one"
        assert results[0]["snippet"] == "Bing snippet one."

    def test_chinese_engines_are_parsed(self):
        baidu = builtin._parse_baidu(BAIDU_PAGE, 5)
        assert [r["title"] for r in baidu] == ["阶跃星辰 Agent", "第二条结果"]
        assert baidu[0]["url"].startswith("http://www.baidu.com/link?url=")
        assert baidu[0]["snippet"] == "阶跃星辰发布 Agent 产品。"

        so = builtin._parse_so(SO_PAGE, 5)
        assert [r["title"] for r in so] == ["阶跃星辰 Agent", "第二条"]
        assert so[0]["url"] == "https://example.com/so-one"
        assert so[0]["snippet"] == "360 搜索摘要。"

        sogou = builtin._parse_sogou(SOGOU_PAGE, 5)
        assert [r["title"] for r in sogou] == ["阶跃星辰 Agent", "第二条"]
        assert sogou[0]["url"] == "https://example.com/sogou-one"
        assert sogou[0]["snippet"] == "搜狗摘要。"

    def test_a_page_that_does_not_match_yields_nothing(self):
        """Rather than raising — the caller falls through to the next engine."""
        assert builtin._parse_duckduckgo("<html>consent page</html>", 5) == []
        assert builtin._parse_bing("<html>consent page</html>", 5) == []
        assert builtin._parse_baidu("<html>consent page</html>", 5) == []
        assert builtin._parse_so("<html>consent page</html>", 5) == []
        assert builtin._parse_sogou("<html>consent page</html>", 5) == []


# ---------------------------------------------------------------------------
# 2. Engine selection
# ---------------------------------------------------------------------------


class TestChoosingAnEngine:
    async def test_the_primary_engine_is_used_when_it_answers(self, monkeypatch):
        async def fake(url: str, **_: Any) -> dict[str, Any]:
            return _page(DUCKDUCKGO_PAGE)

        monkeypatch.setattr(builtin, "_http_request", fake)
        reply = await builtin._web_search("anything")

        assert reply["engine"] == "duckduckgo"
        assert "degraded" not in reply
        assert len(reply["results"]) == 2

    async def test_chinese_queries_start_with_baidu(self, monkeypatch):
        async def fake(url: str, **_: Any) -> dict[str, Any]:
            if "baidu.com" in url:
                return _page(BAIDU_PAGE)
            return _page("<html>not used</html>")

        monkeypatch.setattr(builtin, "_http_request", fake)
        reply = await builtin._web_search("阶跃星辰 Agent")

        assert reply["engine"] == "baidu"
        assert "degraded" not in reply
        assert reply["results"][0]["title"] == "阶跃星辰 Agent"

    async def test_a_useless_primary_falls_through_to_the_next(self, monkeypatch):
        """A page that loads but parses to nothing is a failure, not an answer."""

        async def fake(url: str, **_: Any) -> dict[str, Any]:
            if "duckduckgo" in url:
                return _page("<html>rate limited</html>")
            return _page(BING_PAGE)

        monkeypatch.setattr(builtin, "_http_request", fake)
        reply = await builtin._web_search("anything")

        assert reply["engine"] == "bing"
        assert reply["degraded"] is True
        assert "bing" in reply["detail"]

    async def test_the_engine_that_answered_is_named(self, monkeypatch):
        """So a weaker engine's ranking is never mistaken for the primary's."""

        async def fake(url: str, **_: Any) -> dict[str, Any]:
            if "duckduckgo" in url:
                return {"status": 503, "ok": False, "body": ""}
            return _page(BING_PAGE)

        monkeypatch.setattr(builtin, "_http_request", fake)
        reply = await builtin._web_search("anything")

        assert reply["engine"] == "bing"
        assert reply["degraded"] is True

    async def test_every_engine_failing_is_reported_with_what_was_tried(
        self, monkeypatch
    ):
        async def fake(url: str, **_: Any) -> dict[str, Any]:
            return {"status": 503, "ok": False, "body": ""}

        monkeypatch.setattr(builtin, "_http_request", fake)
        reply = await builtin._web_search("anything")

        assert reply["error"] == "search_failed"
        assert len(reply["attempts"]) == len(builtin._ENGINES)
        assert "hint" in reply


# ---------------------------------------------------------------------------
# 3. Refusals and limits
# ---------------------------------------------------------------------------


class TestRefusalsAndLimits:
    async def test_an_empty_query_never_reaches_the_network(self, monkeypatch):
        async def explode(*_: Any, **__: Any) -> dict[str, Any]:
            raise AssertionError("the network must not be touched")

        monkeypatch.setattr(builtin, "_http_request", explode)
        assert (await builtin._web_search("   "))["error"] == "empty_query"

    async def test_an_egress_denial_is_passed_through_rather_than_hidden(
        self, monkeypatch
    ):
        """The tool must not quietly answer from nothing when policy blocks it."""

        async def fake(url: str, **_: Any) -> dict[str, Any]:
            return {"error": "egress_denied", "detail": "host not permitted"}

        monkeypatch.setattr(builtin, "_http_request", fake)
        reply = await builtin._web_search("anything")

        assert reply["error"] == "search_failed"
        assert any("egress_denied" in attempt for attempt in reply["attempts"])

    async def test_the_result_count_is_capped(self, monkeypatch):
        """The page holds more than the cap, so the cap is what limits it.

        The first version of this test asserted `<= MAX_SEARCH_RESULTS` against a
        page holding two results — true whatever the clamp did. Deleting the
        clamp did not fail it; feeding it an over-full page does.
        """

        async def fake(url: str, **_: Any) -> dict[str, Any]:
            return _page(_duckduckgo_page(builtin.MAX_SEARCH_RESULTS + 2))

        monkeypatch.setattr(builtin, "_http_request", fake)
        reply = await builtin._web_search("anything", max_results=999)

        assert len(reply["results"]) == builtin.MAX_SEARCH_RESULTS

    async def test_the_query_is_url_encoded(self, monkeypatch):
        seen: list[str] = []

        async def fake(url: str, **_: Any) -> dict[str, Any]:
            seen.append(url)
            return _page(DUCKDUCKGO_PAGE)

        monkeypatch.setattr(builtin, "_http_request", fake)
        await builtin._web_search("消费者权益 退货")

        assert seen and "%E6%B6%88%E8%B4%B9%E8%80%85" in seen[0]


# ---------------------------------------------------------------------------
# 4. Registration
# ---------------------------------------------------------------------------


def test_the_tool_is_registered_and_flagged_as_reaching_the_network():
    names = [tool.name for tool in list_tools()]
    assert "web_search" in names
    assert "web_search" in builtin.BUILTIN_TOOLS
    assert get_tool("web_search").dangerous is True
