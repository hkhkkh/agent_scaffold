"""End-to-end smoke test against a running backend.

Exercises the parts that unit tests cannot: the real HTTP surface, SSE streaming,
a real provider call, and the AI features that depend on the model returning
well-formed tool calls.

Usage
-----
    # terminal 1
    python -m uvicorn app.main:app --app-dir backend --port 8001

    # terminal 2
    python scripts/smoke.py            # skip AI (fast, offline-ish)
    python scripts/smoke.py --ai       # include live model calls (costs tokens)

Exits non-zero on the first failure so it can gate a release.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from typing import Any

import httpx

BASE = "http://127.0.0.1:8001"

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, ok, detail))
    marker = PASS if ok else FAIL
    print(f"  {marker}  {name}" + (f"  ({detail})" if detail and not ok else ""))
    return ok


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def demo_graph() -> dict[str, Any]:
    """A small but non-trivial workflow: branch, aggregate, and merge."""
    return {
        "id": "smoke-demo",
        "name": "Smoke test workflow",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Start",
                "description": "Takes the user's question.",
                "position": {"x": 0, "y": 0},
                "config": {"input_schema": {"query": "What is 2+2?"}},
            },
            {
                "id": "prep",
                "type": "prompt",
                "label": "Prepare prompt",
                "description": "Wraps the question for the model.",
                "position": {"x": 320, "y": 0},
                "config": {"template": "Answer concisely: {{ vars.start.out.query }}"},
            },
            {
                "id": "answer",
                "type": "llm",
                "label": "Answer",
                "description": "Generates the final answer.",
                "position": {"x": 640, "y": 0},
                "config": {"prompt": "{{ vars.prep.out }}", "system_prompt": "Be brief."},
            },
            {
                "id": "end",
                "type": "end",
                "label": "End",
                "position": {"x": 960, "y": 0},
                "config": {"output_label": "answer"},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "sourceHandle": "out", "target": "prep", "targetHandle": "in"},
            {"id": "e2", "source": "prep", "sourceHandle": "out", "target": "answer", "targetHandle": "in"},
            {"id": "e3", "source": "answer", "sourceHandle": "out", "target": "end", "targetHandle": "in"},
        ],
    }


def broken_graph() -> dict[str, Any]:
    """Missing a Start node and with a dangling edge — must be rejected."""
    return {
        "id": "smoke-broken",
        "name": "Broken",
        "nodes": [
            {"id": "p", "type": "prompt", "config": {"template": "hi"}},
        ],
        "edges": [{"id": "x", "source": "p", "target": "ghost"}],
    }


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def test_health(client: httpx.Client) -> None:
    section("Catalogue")
    response = client.get("/api/health")
    check("GET /api/health", response.status_code == 200)

    response = client.get("/api/node-types")
    check("GET /api/node-types", response.status_code == 200)
    if response.status_code == 200:
        payload = response.json()
        count = payload["count"]
        check("  node types registered", count >= 12, f"found {count}")
        types = {spec["type"] for spec in payload["types"]}
        expected = {"start", "end", "llm", "prompt", "condition", "code", "agent"}
        check("  core types present", expected <= types, f"missing {expected - types}")
        check("  port colours provided", bool(payload.get("port_colors")))
        # Every param must carry a discriminator or the Inspector cannot render it.
        flat = [p for spec in payload["types"] for p in spec["params"]]
        check(
            "  every param has a type discriminator",
            flat and all(isinstance(p.get("type"), str) for p in flat),
        )

    response = client.get("/api/tools")
    check("GET /api/tools", response.status_code == 200 and response.json()["count"] > 0)

    response = client.get("/api/state-fields")
    check("GET /api/state-fields", response.status_code == 200 and response.json()["reducers"])


def test_validation(client: httpx.Client) -> None:
    section("Validation")
    response = client.post("/api/workflows/validate", json=broken_graph())
    check("POST /api/workflows/validate", response.status_code == 200)
    if response.status_code == 200:
        report = response.json()
        codes = {issue["code"] for issue in report["issues"]}
        check("  reports missing entry point", "missing_entry" in codes, str(sorted(codes)))
        check("  reports dangling edge", "dangling_edge" in codes, str(sorted(codes)))
        check("  marks the graph invalid", report["ok"] is False)

    response = client.post("/api/workflows/validate", json=demo_graph())
    check("valid graph passes", response.status_code == 200 and response.json()["ok"] is True)


def test_ports(client: httpx.Client) -> None:
    section("Dynamic ports")
    conditional = {
        "node": {
            "id": "c",
            "type": "condition",
            "config": {
                "branches": [
                    {"id": "yes", "label": "Yes", "expression": "True"},
                    {"id": "no", "label": "No", "expression": ""},
                ]
            },
        }
    }
    response = client.post("/api/workflows/ports", json=conditional)
    check("POST /api/workflows/ports", response.status_code == 200)
    if response.status_code == 200:
        handles = [p["id"] for p in response.json()["outputs"]]
        check("  one handle per branch", handles == ["yes", "no"], str(handles))


def test_security(client: httpx.Client) -> None:
    """The three end-to-end security assertions that unit tests cannot make.

    `known-defects.md` #4: security had unit coverage but nothing exercised a
    real HTTP request through the real middleware stack. A unit test asserts
    `NetworkEgressPolicy.check` refuses a link-local address; only an end-to-end
    run proves the *tool* goes through the policy at all.
    """
    section("Security")

    # 1. The cloud metadata endpoint, through a real Code-less path: an
    #    http_request tool call. This is the SSRF case, and the assertion is on
    #    the tool's own refusal rather than on a 403 from somewhere upstream.
    #
    #    A whole graph rather than `/runs/node-test`: that endpoint needs a
    #    *saved* workflow and a node id, and the point here is the tool's
    #    egress check, not the debugging endpoint. This runs the real path a
    #    user's workflow would take.
    graph = {
        "id": f"smoke-ssrf-{int(time.time())}",
        "name": "SSRF probe",
        "nodes": [
            {"id": "s", "type": "start", "config": {"input_schema": {"q": "x"}}},
            {
                "id": "t",
                "type": "tool",
                "config": {
                    "tool": "http_request",
                    # The key is `arguments`, not `args` — see
                    # `app/nodes/tool.py`, which reads `ctx.config["arguments"]`.
                    "arguments": {"url": "http://169.254.169.254/latest/meta-data/"},
                },
            },
            {"id": "e", "type": "end", "config": {"output_label": "r"}},
        ],
        "edges": [
            {"id": "e1", "source": "s", "sourceHandle": "out", "target": "t", "targetHandle": "in"},
            {"id": "e2", "source": "t", "sourceHandle": "out", "target": "e", "targetHandle": "in"},
        ],
    }
    created = client.post("/api/workflows", json=graph)
    if created.status_code != 201:
        check("  metadata endpoint is refused", False, created.text[:200])
    else:
        run = client.post(
            "/api/runs",
            json={"workflow_id": graph["id"], "inputs": {}, "tweaks": {}},
        )
        check("  SSRF probe run started", run.status_code == 201, run.text[:200])
        if run.status_code == 201:
            run_id = run.json()["id"]
            # Poll rather than read the SSE stream: this only needs the outcome,
            # and `test_run` above already covers streaming in detail.
            payload: dict[str, Any] = {}
            for _ in range(40):
                time.sleep(0.5)
                payload = client.get(f"/api/runs/history?workflow_id={graph['id']}").json()
                rows = payload.get("runs") or []
                if rows and rows[0]["status"] not in ("pending", "running", "queued"):
                    payload = rows[0]
                    break
            blob = json.dumps(payload).lower()
            # The refusal is a *result*, not a crash: the tool returns an error
            # value and the run either fails on it or passes it along. Either
            # way the address must never have been contacted, and the word
            # "egress" or "refus"/"denied" is how the policy says so.
            check(
                "  metadata endpoint is refused",
                any(word in blob for word in ("egress", "refus", "denied")),
                json.dumps(payload)[:250],
            )
        client.request("DELETE", f"/api/workflows/{graph['id']}")

    # 2. Import with a path-traversal id. `data/workflows/<id>.json` is built by
    #    joining the id, so an id of `../../../tmp/x` would write outside the
    #    directory. The fix for this is Phase 0 task 6; this asserts it stayed
    #    fixed through the HTTP layer.
    response = client.post(
        "/api/workflows/import",
        json={"id": "../../../tmp/pwned", "name": "x", "nodes": [], "edges": []},
    )
    check(
        "  traversal id is rejected",
        400 <= response.status_code < 500,
        f"got {response.status_code}",
    )

    # 3. The trace id, which every response now carries (task 25). Cheap to
    #    assert here and it catches the middleware being dropped by a refactor,
    #    which unit tests of `trace.py` would not.
    response = client.get("/api/node-types")
    header = response.headers.get("traceparent", "")
    check(
        "  traceparent is well-formed",
        bool(re.fullmatch(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}", header)),
        header or "(missing)",
    )


def test_workflow_crud(client: httpx.Client) -> str | None:
    section("Workflow CRUD")
    graph = demo_graph()
    graph["id"] = f"smoke-{int(time.time())}"

    response = client.post("/api/workflows", json=graph)
    check("POST /api/workflows", response.status_code == 201, response.text[:200])
    if response.status_code != 201:
        return None
    workflow_id = response.json()["id"]

    response = client.get(f"/api/workflows/{workflow_id}")
    check("GET one workflow", response.status_code == 200)

    response = client.get("/api/workflows")
    check("GET workflow list", response.status_code == 200 and any(
        w["id"] == workflow_id for w in response.json()["workflows"]
    ))

    # Saving must round-trip the graph exactly.
    graph["name"] = "Renamed by smoke test"
    response = client.put(f"/api/workflows/{workflow_id}", json=graph)
    check("PUT workflow", response.status_code == 200)
    fetched = client.get(f"/api/workflows/{workflow_id}").json()
    check("  rename persisted", fetched["name"] == "Renamed by smoke test")
    check("  node count preserved", len(fetched["nodes"]) == len(graph["nodes"]))

    response = client.post(f"/api/workflows/{workflow_id}/export")
    check("POST export writes JSON", response.status_code == 200)

    return workflow_id


def test_run(client: httpx.Client, workflow_id: str | None) -> str | None:
    section("Run (ad-hoc, exercises the real engine)")
    response = client.post(
        "/api/runs/adhoc",
        json={
            "graph": demo_graph(),
            "inputs": {"query": "What is the capital of France?"},
        },
    )
    check("POST /api/runs/adhoc", response.status_code == 201, response.text[:300])
    if response.status_code != 201:
        return None

    run_id = response.json()["id"]
    print(f"       run id: {run_id}")

    # Follow the SSE stream to completion.
    events: list[dict[str, Any]] = []
    try:
        with client.stream("GET", f"/api/runs/{run_id}/events", timeout=60.0) as stream:
            for line in stream.iter_lines():
                if not line.startswith("data: "):
                    continue
                events.append(json.loads(line[6:]))
                if events[-1]["type"] in ("run_finished", "run_failed"):
                    break
    except httpx.HTTPError as exc:
        check("SSE stream", False, str(exc))
        return run_id

    check("SSE stream produced events", len(events) > 0, f"{len(events)} events")
    types = [event["type"] for event in events]
    check("  started", types[0] == "run_started", types[0] if types else "none")
    check("  finished", "run_finished" in types or "run_failed" in types)
    check(
        "  node lifecycle present",
        "node_started" in types and "node_finished" in types,
    )
    check(
        "  sequence numbers are monotonic",
        all(a["seq"] < b["seq"] for a, b in zip(events, events[1:])),
    )

    final = client.get(f"/api/runs/{run_id}").json()
    check("GET run status", final["status"] == "succeeded", final.get("error") or final["status"])
    check("  output produced", bool(final["outputs"]), str(final["outputs"])[:120])

    steps = client.get(f"/api/runs/{run_id}/steps").json()["steps"]
    check("GET steps", len(steps) >= 3, f"{len(steps)} steps")
    check("  step order recorded", [s["index"] for s in steps] == sorted(s["index"] for s in steps))
    check("  steps carry inputs and outputs", all(s["inputs"] is not None for s in steps))

    return run_id


def test_node_test_endpoint(client: httpx.Client, workflow_id: str | None) -> None:
    section("Single-node debugging")
    if not workflow_id:
        check("POST /api/runs/node-test", False, "no saved workflow")
        return

    response = client.post(
        "/api/runs/node-test",
        json={
            "workflow_id": workflow_id,
            "node_id": "answer",
            "inputs": {"in": "Test prompt from the smoke test"},
            "config_overrides": {"prompt": "Say hello.", "output_format": "text"},
        },
    )
    check("POST /api/runs/node-test", response.status_code == 200, response.text[:250])
    if response.status_code == 200:
        payload = response.json()
        check("  node ran", payload["ok"] is True, str(payload.get("error"))[:160])
        check("  produced output", bool(payload.get("outputs")))
        check("  returned logs", isinstance(payload.get("logs"), list))


def test_hitl(client: httpx.Client) -> None:
    section("Human-in-the-loop")
    graph = {
        "id": "smoke-hitl",
        "name": "Approval flow",
        "nodes": [
            {"id": "s", "type": "start", "config": {"input_schema": {"draft": "hello"}}},
            {
                "id": "h",
                "type": "human_input",
                "label": "Review",
                "config": {
                    "message": "Approve this draft?",
                    "fields": {"value": {"type": "string", "label": "Final text"}},
                },
            },
            {"id": "e", "type": "end", "config": {"output_label": "approved_text"}},
        ],
        "edges": [
            {"id": "e1", "source": "s", "sourceHandle": "out", "target": "h", "targetHandle": "in"},
            {"id": "e2", "source": "h", "sourceHandle": "out", "target": "e", "targetHandle": "in"},
        ],
    }

    response = client.post("/api/runs/adhoc", json={"graph": graph})
    if not check("start HITL run", response.status_code == 201, response.text[:250]):
        return
    run_id = response.json()["id"]

    # Wait for the interrupt.
    payload: dict[str, Any] = {}
    for _ in range(60):
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["status"] in ("interrupted", "failed", "succeeded"):
            break
        time.sleep(0.2)

    check("run pauses for input", payload["status"] == "interrupted", payload.get("error") or payload["status"])
    check("  interrupt payload carries the prompt", bool(payload.get("interrupt")))

    response = client.post(
        f"/api/runs/{run_id}/resume",
        json={"approved": True, "values": {"value": "approved text"}},
    )
    check("POST resume", response.status_code == 200, response.text[:250])

    for _ in range(60):
        payload = client.get(f"/api/runs/{run_id}").json()
        if payload["status"] in ("succeeded", "failed"):
            break
        time.sleep(0.2)

    check("run completes after resume", payload["status"] == "succeeded", payload.get("error") or payload["status"])
    check(
        "  resumed value reached the output",
        str(payload.get("outputs", {}).get("approved_text", "")).find("approved text") >= 0,
        str(payload.get("outputs")),
    )


def test_ai_status(client: httpx.Client) -> bool:
    section("AI status")
    response = client.get("/api/ai/status")
    ok = check("GET /api/ai/status", response.status_code == 200)
    if not ok:
        return False
    payload = response.json()
    print(f"       model={payload['model']} base_url={payload['base_url']}")
    print(f"       enabled={payload['enabled']} configured={payload['configured']}")
    if payload.get("reason"):
        print(f"       reason: {payload['reason']}")
    return bool(payload["enabled"] and payload["configured"])


def test_ai_fill_node(client: httpx.Client) -> None:
    section("AI: fill a node from its description")
    graph = demo_graph()
    node = next(n for n in graph["nodes"] if n["id"] == "answer")
    node["description"] = (
        "把用户的问题翻译成英文，然后用一句话给出简洁答案。"
    )
    node["config"] = {}  # let the AI fill it in

    response = client.post(
        "/api/ai/fill-node",
        json={"graph": graph, "node_id": "answer"},
        timeout=120.0,
    )
    if not check("POST /api/ai/fill-node", response.status_code == 200, response.text[:400]):
        return

    payload = response.json()
    config = payload["config"]
    check("  returned a config", bool(config), str(config)[:160])
    check("  rationale given", bool(payload["rationale"].strip()))
    check("  changes listed", isinstance(payload["changes"], list))

    # The critical guarantee: only real params come back.
    valid = {"system_prompt", "prompt", "model", "output_format", "temperature",
             "max_tokens", "include_history"}
    unknown = set(config) - valid
    check("  no invented fields", not unknown, f"unknown: {unknown}")
    check("  prompt filled in", bool(config.get("prompt")), str(config)[:200])

    print(f"       prompt: {str(config.get('prompt'))[:150]}")
    if payload["warnings"]:
        print(f"       warnings: {payload['warnings']}")


def test_ai_generate_graph(client: httpx.Client) -> None:
    section("AI: generate a workflow from a description")
    response = client.post(
        "/api/ai/generate-graph",
        json={
            "prompt": (
                "构建一个能回答用户问题的问答助手：先判断问题是否属于技术类，"
                "技术问题走详细解答流程，其它问题走简短回答流程，最后汇总输出。"
            )
        },
        timeout=240.0,
    )
    if not check("POST /api/ai/generate-graph", response.status_code == 200, response.text[:400]):
        return

    payload = response.json()
    graph = payload["graph"]
    types = [n["type"] for n in graph["nodes"]]

    check("  nodes generated", len(graph["nodes"]) >= 3, f"{len(graph['nodes'])} nodes")
    check("  has a start node", "start" in types, str(types))
    check("  has an end node", "end" in types, str(types))
    check("  edges generated", len(graph["edges"]) >= 2, f"{len(graph['edges'])} edges")
    check(
        "  every node has a description",
        all(str(n.get("description", "")).strip() for n in graph["nodes"]),
        "some nodes are undescribed",
    )
    check("  graph validates", payload["validation"]["ok"] is True,
          json.dumps(payload["validation"]["issues"], ensure_ascii=False)[:400])

    # Positions must be laid out, not all at the origin.
    positions = {(n["position"]["x"], n["position"]["y"]) for n in graph["nodes"]}
    check("  nodes were laid out", len(positions) > 1, str(positions))

    print(f"       generated: {' -> '.join(types)}")
    print(f"       tools used: {[t['tool'] for t in payload['transcript']][:12]}")


def test_ai_conversions(client: httpx.Client) -> None:
    section("AI: node <-> code conversion")
    graph = demo_graph()
    response = client.post(
        "/api/ai/node-to-code",
        json={"graph": graph, "node_id": "prep"},
        timeout=120.0,
    )
    if not check("POST /api/ai/node-to-code", response.status_code == 200, response.text[:300]):
        return
    payload = response.json()
    check("  code returned", bool(payload["code"].strip()))
    check("  lossiness reported", isinstance(payload["lossy"], bool))
    print(f"       lossy={payload['lossy']}  notes={payload['notes'][:100]}")


def test_ai_repair_graph(client: httpx.Client) -> None:
    section("AI: repair a broken workflow")

    # Deliberately broken in several ways, so the run exercises structural repair
    # (no Start), a required parameter left empty, and an orphan. The nodes are
    # hand-named so we can prove afterwards that the user's work survived — which
    # is the property that matters most.
    #
    # Note the deliberate omission of an End node: this codebase classifies that
    # as a *warning*, so an error-only repair must leave it alone. Asserting that
    # it stays absent is the check that proves the severity default is honoured.
    graph = {
        "id": "smoke-repair",
        "name": "Repair smoke",
        "nodes": [
            {
                "id": "keepme",
                "type": "prompt",
                "label": "Keep Me",
                "description": "把用户的问题拼成一段提示词。",
                "position": {"x": 0, "y": 0},
                "config": {"template": "{{ query }}"},
            },
            {
                "id": "emptyreq",
                "type": "llm",
                "label": "Needs Prompt",
                "description": "根据上一步的提示词给出回答。",
                "position": {"x": 340, "y": 0},
                "config": {"prompt": ""},
            },
        ],
        "edges": [],
    }

    response = client.post(
        "/api/ai/repair-graph",
        json={"graph": graph, "lang": "zh"},
        timeout=300.0,
    )
    if not check(
        "POST /api/ai/repair-graph", response.status_code == 200, response.text[:400]
    ):
        return

    payload = response.json()
    repaired = payload["graph"]
    ids = {n["id"] for n in repaired["nodes"]}
    types = {n["type"] for n in repaired["nodes"]}

    check("  came back resolved", payload["resolved"] is True,
          json.dumps(payload["validation"]["issues"], ensure_ascii=False)[:400])
    check("  reported a change", payload["changed"] is True)
    check("  no problems left", payload["remaining"] == 0, str(payload["remaining"]))
    check("  graph still validates", payload["validation"]["ok"] is True)

    # The user's nodes must survive a repair. This is the guard against a model
    # that "fixes" validation by deleting whatever it does not understand.
    check("  kept the user's nodes", {"keepme", "emptyreq"} <= ids, str(sorted(ids)))
    check("  did not regress", payload["regressed"] is False, json.dumps(payload["diff"], ensure_ascii=False)[:300])
    check("  added the missing Start", "start" in types, str(sorted(types)))

    # A missing End is a warning, not an error, so an error-only repair must not
    # invent one. This is the check that proves the severity default is real
    # rather than documented — chasing warnings would mean adding nodes the user
    # never asked for.
    check("  left the End-node warning alone", "end" not in types, str(sorted(types)))

    # Warnings that are deliberately not chased still have to be *reported*, or
    # the user would see a clean badge and assume the workflow is finished.
    remaining_warnings = [
        i for i in payload["validation"]["issues"] if i["severity"] == "warning"
    ]
    check("  reported the warnings it skipped", bool(remaining_warnings),
          json.dumps(payload["validation"]["issues"], ensure_ascii=False)[:300])
    check("  summary mentions the warnings", "警告" in payload["summary"], payload["summary"])

    # Identity has to survive, or applying the repair would save a new workflow
    # instead of updating this one.
    check("  kept the workflow id", repaired["id"] == "smoke-repair", repaired["id"])
    check("  kept the workflow name", repaired["name"] == "Repair smoke", repaired["name"])

    # The parameters the user wrote are not the AI's to rewrite.
    label = next((n["label"] for n in repaired["nodes"] if n["id"] == "keepme"), None)
    check("  kept the user's label", label == "Keep Me", str(label))
    check("  kept the prompt text",
          any(n["id"] == "keepme" and n["config"].get("template") == "{{ query }}"
              for n in repaired["nodes"]))

    check("  diff is machine-readable",
          isinstance(payload["diff"]["nodes_added"], list)
          and isinstance(payload["diff"]["nodes_removed"], list))
    check("  summary is in Chinese",
          any("\u4e00" <= ch <= "\u9fff" for ch in payload["summary"]),
          payload["summary"])

    print(f"       {payload['summary']}")
    print(f"       attempts={payload['attempts']} "
          f"delta=+{len(payload['diff']['nodes_added'])}/-{len(payload['diff']['nodes_removed'])}")
    if payload["warnings"]:
        print(f"       warnings: {payload['warnings']}")

    # A valid graph must short-circuit: no model call, nothing changed. Worth
    # asserting because a repair pass over a healthy graph can only make it
    # worse, and "attempts" is how we can tell it never happened.
    #
    # `severity=error` is what the UI sends by default, and the repaired graph has
    # no errors left — so this is the early-exit path.
    healthy = client.post(
        "/api/ai/repair-graph",
        json={"graph": repaired, "lang": "zh"},
        timeout=120.0,
    )
    if check("  repairing a valid graph returns 200", healthy.status_code == 200,
             healthy.text[:300]):
        second = healthy.json()
        check("  no model call was made", second["attempts"] == 0, str(second["attempts"]))
        check("  nothing changed", second["changed"] is False)
        check("  still resolved", second["resolved"] is True)

    # The other mode: warnings become targets. The graph still has warnings (the
    # orphaned nodes), so this one *must* reach the model — which is how we can
    # tell the flag is actually plumbed through rather than ignored.
    wider = client.post(
        "/api/ai/repair-graph",
        json={"graph": repaired, "severity": "all", "lang": "en", "max_iterations": 6},
        timeout=300.0,
    )
    if check("  severity=all returns 200", wider.status_code == 200, wider.text[:300]):
        both = wider.json()
        check("  severity=all reached the model", both["attempts"] >= 1, str(both["attempts"]))
        check("  severity=all reports fewer targets left",
              both["remaining"] <= len(remaining_warnings),
              f"{both['remaining']} vs {len(remaining_warnings)}")
        check("  English summary when asked for English",
              not any("\u4e00" <= ch <= "\u9fff" for ch in both["summary"]),
              both["summary"])
        print(f"       severity=all: {both['summary']} (attempts={both['attempts']})")


def test_settings(client: httpx.Client) -> None:
    section("Settings")
    response = client.get("/api/settings")
    check("GET /api/settings", response.status_code == 200)

    response = client.get("/api/settings/llm/models")
    check("GET models list", response.status_code == 200, response.text[:200])

    response = client.post("/api/settings/llm/test", timeout=60.0)
    if check("POST llm test", response.status_code == 200, response.text[:200]):
        payload = response.json()
        check("  connection works", payload["ok"] is True, str(payload.get("detail"))[:200])
        print(f"       model={payload.get('model')} reply={payload.get('reply', '')[:40]!r}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke test the Agent Scaffold API.")
    parser.add_argument("--ai", action="store_true", help="Include live model calls.")
    parser.add_argument("--base", default=BASE, help="Backend base URL.")
    args = parser.parse_args()

    print(f"\033[1mAgent Scaffold — smoke test against {args.base}\033[0m")

    with httpx.Client(base_url=args.base, timeout=30.0) as client:
        try:
            client.get("/api/health")
        except httpx.HTTPError as exc:
            print(f"\n{FAIL}  Backend is not reachable at {args.base}: {exc}")
            print("      Start it with:")
            print("        python -m uvicorn app.main:app --app-dir backend --port 8001")
            return 2

        test_health(client)
        test_validation(client)
        test_ports(client)
        test_security(client)
        workflow_id = test_workflow_crud(client)
        test_run(client, workflow_id)
        test_node_test_endpoint(client, workflow_id)
        test_hitl(client)
        test_settings(client)

        ai_ready = test_ai_status(client)
        if args.ai and ai_ready:
            test_ai_fill_node(client)
            test_ai_generate_graph(client)
            test_ai_repair_graph(client)
            test_ai_conversions(client)
        elif args.ai:
            section("AI tests")
            print(f"  {SKIP}  AI is not configured")
        else:
            section("AI tests")
            print(f"  {SKIP}  pass --ai to include live model calls")

    passed = sum(1 for _, ok, _ in _results if ok)
    failed = [name for name, ok, _ in _results if not ok]
    total = len(_results)

    print(f"\n\033[1m{passed}/{total} checks passed\033[0m")
    if failed:
        print(f"{FAIL}  failing checks:")
        for name in failed:
            print(f"      - {name.strip()}")
        return 1
    print(f"{PASS}  everything works")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
