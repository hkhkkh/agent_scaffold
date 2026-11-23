"""Re-measure the reuse numbers quoted in `docs/target/reuse.md` §9.5.

The figures there are claims about the code as it is now, and prose drifts. This
recomputes them so the doc can be checked rather than trusted:

    python scripts/measure_reuse.py

It prints the same table the doc shows. If a number disagrees, the doc is stale —
fix the doc, not this script.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

#: Where the account that backs the doc lives. `langgraph` is a *source* install
#: (see task 27c), so its numbers cannot come from site-packages.
LANGGRAPH_SOURCE = pathlib.Path(r"H:\agent_1\langgraph")

PACKAGES = (
    "langgraph",
    "langchain",
    "langchain_core",
    "langchain_openai",
    "fastapi",
    "starlette",
    "pydantic",
    "sqlmodel",
    "sqlalchemy",
    "sse_starlette",
    "aiosqlite",
)

#: Files we wrote for the memory and goals features, and the point of the
#: comparison: everything under `upstream` is what we did not write.
UPSTREAM_STORE = "libs/checkpoint/langgraph/store"
OUR_STORE_FILES = (
    "app/memory/store.py",
    "app/memory/nodes.py",
    "app/memory/goals.py",
    "app/memory/goals_node.py",
    "app/memory/__init__.py",
)


def lines(path: pathlib.Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="replace").splitlines())


def package_lines(package: str) -> tuple[int, int]:
    spec = importlib.util.find_spec(package)
    if spec is None or not spec.submodule_search_locations:
        return (0, 0)
    root = pathlib.Path(list(spec.submodule_search_locations)[0])
    files = [
        f
        for f in root.rglob("*.py")
        if "__pycache__" not in f.parts and "tests" not in f.parts
    ]
    return (len(files), sum(lines(f) for f in files))


def main() -> int:
    backend = pathlib.Path(__file__).resolve().parents[1]

    print("=== upstream packages we depend on ===")
    upstream_files = upstream_lines = 0
    for package in PACKAGES:
        count, total = package_lines(package)
        upstream_files += count
        upstream_lines += total
        print(f"  {package:20} {count:5} files {total:9,} lines")
    print(f"  {'TOTAL':20} {upstream_files:5} files {upstream_lines:9,} lines")

    app_files = [f for f in (backend / "app").rglob("*.py")]
    app_lines = sum(lines(f) for f in app_files)
    print()
    print(f"  ours (app/)                    {app_lines:9,} lines")
    if app_lines:
        print(f"  ratio                          {upstream_lines / app_lines:9.1f}x")

    print()
    print("=== the store capability both features stand on ===")
    store_files: list[pathlib.Path] = []
    for base in (LANGGRAPH_SOURCE / UPSTREAM_STORE, *(
        pathlib.Path(list(spec.submodule_search_locations)[0]) / "store"
        for spec in [importlib.util.find_spec("langgraph")]
        if spec and spec.submodule_search_locations
    )):
        if base.is_dir():
            store_files = [
                f for f in base.rglob("*.py") if "__pycache__" not in f.parts
            ]
            print(f"  measured from: {base}")
            break
    if not store_files:
        print("  PROBLEM: could not find the store package")
        return 1

    upstream_store = sum(lines(f) for f in store_files)
    for path in sorted(store_files):
        print(f"      {lines(path):6,}  {path.name}")

    ours = 0
    print()
    print("=== ours ===")
    for rel in OUR_STORE_FILES:
        path = backend / rel
        if path.exists():
            count = lines(path)
            ours += count
            print(f"      {count:6,}  {rel}")
    print()
    print(f"  upstream store : {upstream_store:6,} lines")
    print(f"  our memory code: {ours:6,} lines")
    if ours:
        print(f"  ratio          : {upstream_store / ours:.1f}x")

    print()
    print("Compare against docs/target/reuse.md §9.5. A mismatch means the doc")
    print("is stale; the numbers here are measured, the doc is a copy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
