"""Build, run and export the Excel chat demo agent.

What this proves, in order:

1. a workflow that uses the **files capability pack** can be saved and run on the
   canvas (the pack's tools are in this deployment's registry);
2. the run really reads a spreadsheet from the workspace -- the sample workbook is
   generated here, uploaded through `/api/files`, and the agent is asked about it;
3. the same workflow **exports into a standalone package** whose `manifest.yml`
   names the `files` pack, and whose runtime carries the same implementations.

Usage:

    python scripts/demo/excel_chat_demo.py                # build + run + export
    python scripts/demo/excel_chat_demo.py --skip-run     # build + export only
    python scripts/demo/excel_chat_demo.py --message "哪个产品卖得最多？"

The exported package is written under `DATA_DIR/exports/excel-chat-demo` by the
platform's own exporter. To chat with it:

    cd <exported-package>
    python main.py serve --http --port 8080
    # then open http://127.0.0.1:8080 and upload a workbook in the page

This script is deliberately stdlib-only (urllib + openpyxl for the sample file)
so it keeps working on a machine where the frontend toolchain is not installed.
"""

from __future__ import annotations

import argparse
import base64
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORKFLOW_FILE = HERE / "excel_chat_agent.json"
SAMPLE_NAME = "sales_demo.xlsx"


def api(base: str, method: str, path: str, payload: dict | None = None) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc


def make_sample_workbook(target: pathlib.Path) -> None:
    """A tiny sales table, in Chinese, so the demo needs no external fixture."""
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "订单"
    sheet.append(["订单号", "产品", "数量", "单价", "金额", "日期"])
    rows = [
        ("SO-1001", "笔记本电脑", 3, 6800, 20400, "2026-10-01"),
        ("SO-1002", "显示器", 8, 1200, 9600, "2026-10-02"),
        ("SO-1003", "机械键盘", 15, 450, 6750, "2026-10-03"),
        ("SO-1004", "笔记本电脑", 5, 6800, 34000, "2026-10-05"),
        ("SO-1005", "显示器", 6, 1200, 7200, "2026-10-06"),
        ("SO-1006", "无线鼠标", 30, 120, 3600, "2026-10-07"),
        ("SO-1007", "笔记本电脑", 2, 6800, 13600, "2026-10-09"),
        ("SO-1008", "机械键盘", 20, 450, 9000, "2026-10-10"),
        ("SO-1009", "显示器", 4, 1200, 4800, "2026-10-12"),
        ("SO-1010", "无线鼠标", 25, 120, 3000, "2026-10-13"),
    ]
    for row in rows:
        sheet.append(list(row))
    for column, width in zip("ABCDEF", (12, 14, 8, 10, 12, 14)):
        sheet.column_dimensions[column].width = width
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)


def upload_sample(base: str, workbook: pathlib.Path) -> None:
    encoded = base64.b64encode(workbook.read_bytes()).decode("ascii")
    result = api(
        base,
        "POST",
        "/api/files",
        {"name": SAMPLE_NAME, "content_base64": encoded},
    )
    print(f"上传样例表格 / uploaded: {result.get('name')} ({result.get('size')} bytes)")


def save_workflow(base: str) -> dict:
    graph = json.loads(WORKFLOW_FILE.read_text(encoding="utf-8"))
    try:
        saved = api(base, "POST", "/api/workflows", graph)
        print(f"创建工作流 / created workflow: {saved.get('id')}")
    except SystemExit:
        # Already saved from an earlier run: saving over it keeps the same id,
        # which is what the export and the version history expect.
        saved = api(base, "PUT", f"/api/workflows/{graph['id']}", graph)
        print(f"更新工作流 / updated workflow: {saved.get('id')}")
    return saved


def run_workflow(base: str, workflow_id: str, message: str) -> dict:
    started = api(
        base,
        "POST",
        "/api/runs",
        {
            "workflow_id": workflow_id,
            "inputs": {"message": message, "history": ""},
        },
    )
    run_id = started.get("id")
    print(f"运行已启动 / run started: {run_id}")
    deadline = time.time() + 300
    last_status = None
    while time.time() < deadline:
        state = api(base, "GET", f"/api/runs/{run_id}")
        status = state.get("status")
        if status != last_status:
            print(f"  status: {status}")
            last_status = status
        if status in ("succeeded", "failed", "stopped", "cancelled"):
            return state
        time.sleep(1.5)
    raise SystemExit(f"run {run_id} did not finish within 300s")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8001")
    parser.add_argument(
        "--message",
        default="这份表格里哪个产品卖得最多？总金额是多少？请给出依据。",
        help="question to ask in the platform run",
    )
    parser.add_argument("--skip-run", action="store_true", help="do not run in the canvas")
    parser.add_argument("--skip-export", action="store_true", help="do not export the package")
    args = parser.parse_args(argv)

    base = args.base.rstrip("/")
    health = api(base, "GET", "/api/health")
    if health.get("status") != "ok":
        return 2

    workbook = REPO / "data" / "demo" / SAMPLE_NAME
    make_sample_workbook(workbook)
    upload_sample(base, workbook)

    saved = save_workflow(base)
    workflow_id = saved["id"]

    if not args.skip_run:
        state = run_workflow(base, workflow_id, args.message)
        outputs = state.get("outputs") or {}
        print("运行结果 / outputs:")
        print(json.dumps(outputs, ensure_ascii=False, indent=2)[:4000])
        if state.get("error"):
            print("error:", state["error"])

    if not args.skip_export:
        exported = api(base, "POST", f"/api/workflows/{workflow_id}/export-agent")
        print(f"导出包 / package: {exported.get('path')}")
        print(f"  files: {len(exported.get('files') or [])}")
        print(f"  runtime_hash: {exported.get('runtime_hash')}")
        print("下一步 / next:")
        print(f"  cd {exported.get('path')}")
        print("  python main.py doctor")
        print("  python main.py serve --http --port 8080")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
