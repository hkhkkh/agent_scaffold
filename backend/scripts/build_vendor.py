"""Build the sealed dependency set an exported agent installs from.

Run at release time, on a machine with a network:

    python scripts/build_vendor.py

What it produces, and where
---------------------------
``backend/_vendor/``               the wheels, one per dependency in the closure
``backend/_vendor_requirements.txt``  every wheel pinned to an exact version with
                                      a ``--hash=sha256:``

The exporter reads the lock file when it is present and writes a *sealed*
``requirements.txt`` into the agent package; the customer then installs with::

    pip install --no-index --find-links vendor/ --require-hashes -r requirements.txt

which is what turns "every agent shares one runtime" from a promise into a fact —
pip refuses a wheel whose bytes differ from the hash.

Two details that are load-bearing, both measured rather than assumed
-------------------------------------------------------------------
**``--only-binary=:all:`` is not a convenience.** pip's own docs recommend it
alongside hash-checking mode, and `reuse.md` §1.36 records why: pip does not
verify the build dependencies in ``[build-system].requires`` when it builds an
sdist, so an sdist in the closure is a hole in the seal. Failed here as ``count
!= 0`` rather than a warning, because a seal with a gap is not a seal.

**langgraph is built from source, not downloaded.** PyPI's ``1.2.11`` wheel
differs from the one this project runs against: its ``pregel/_utils.py`` is 291
lines and detects subgraphs by reading source text, while the source tree's is 221
lines and reads bytecode (commit ``1e44bda``). Task 30 depends on the bytecode
version, so substituting the published wheel would silently change subgraph
behaviour. ``--no-binary langgraph`` is not enough either, since that would fetch
the sdist; the wheel is built locally with ``python -m build`` and copied in.

No network is used after this script finishes — that is the point of vendoring.
"""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1]
VENDOR = BACKEND / "_vendor"
LOCK = BACKEND / "_vendor_requirements.txt"
BUILD_TMP = BACKEND / "_wheelbuild"

#: The agent's direct dependencies, matching ``export._requirements_txt``. Kept in
#: sync by hand because the two answer different questions: this one is "what do we
#: resolve and hash", that one is "what does the package declare".
DIRECT = (
    "langgraph==1.2.11",
    "langchain-core>=1.0,<2.0",
    "langchain>=1.0,<2.0",
    "langchain-openai>=1.0,<2.0",
    "pydantic>=2.0,<3.0",
    "pydantic-settings>=2.0,<3.0",
    # `main.py` loads `agent.yaml`, and `!ref` needs a YAML parser.
    "pyyaml>=6.0,<7.0",
    # The files capability pack. Its spreadsheet / Word / PDF readers are
    # imported lazily, so a deployment that never uses the pack never needs
    # them -- but an agent whose workflow does use it cannot start without
    # them, and the seal has to cover what an exported agent actually imports.
    # The export test walks every import, including the ones inside functions,
    # precisely so a lazy dependency cannot slip past this list.
    "openpyxl>=3.1,<4.0",
    "python-docx>=1.1,<2.0",
    "pypdf>=5,<7.0",
    # The sqlite checkpointer, and the driver it opens its connection with.
    # Both are named only by *lazy* imports inside `runtime/checkpoints.py`, so
    # nothing at module level drags them into the closure — and nothing else in
    # it depends on them either. Leave either out and the export still builds,
    # still passes `selfcheck`, and then dies with ImportError the first time an
    # agent is actually run with `CHECKPOINT_STORE=sqlite`. `sqlite-vec` arrives
    # with the first one.
    "langgraph-checkpoint-sqlite>=3.0,<4.0",
    "aiosqlite>=0.20,<1.0",
)

#: Built from a source tree instead of downloaded, with the reason above.
#: ``(requirement, source directory)``.
FROM_SOURCE = (
    ("langgraph==1.2.11", pathlib.Path("H:/agent_1/langgraph/libs/langgraph")),
)


def _run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    print("$", " ".join(args))
    return subprocess.run(args, check=True, text=True, **kwargs)


def _download_closure() -> None:
    if VENDOR.exists():
        shutil.rmtree(VENDOR)
    VENDOR.mkdir(parents=True)
    _run(
        [
            sys.executable, "-m", "pip", "download",
            *DIRECT,
            "-d", str(VENDOR),
            # No sdists: pip does not hash-check build requirements, so one would
            # be an unverified build step at install time. See the docstring.
            "--only-binary=:all:",
        ]
    )

    sdists = sorted(p.name for p in VENDOR.iterdir() if p.suffix != ".whl")
    if sdists:
        raise SystemExit(
            f"{len(sdists)} non-wheel artefact(s) in the closure, which "
            f"hash-checking cannot cover: {sdists}. Add a pin or build one."
        )


def _build_from_source() -> None:
    if BUILD_TMP.exists():
        shutil.rmtree(BUILD_TMP)
    BUILD_TMP.mkdir(parents=True)
    for _requirement, source in FROM_SOURCE:
        if not source.exists():
            raise SystemExit(
                f"source tree {source} is missing. It is where the bytecode-based "
                "subgraph detection lives; the published wheel does not have it."
            )
        _run(
            [
                sys.executable, "-m", "build", "--wheel",
                "--outdir", str(BUILD_TMP), str(source),
            ]
        )
    for wheel in sorted(BUILD_TMP.glob("*.whl")):
        # Overwrites the downloaded one of the same version: same name, different
        # bytes, and the hash below is computed from *this* file, so the seal
        # covers what actually ships.
        target = VENDOR / wheel.name
        shutil.copy2(wheel, target)
        print(f"replaced with source build: {target.name}")


def _write_lock() -> None:
    lines = []
    for wheel in sorted(VENDOR.glob("*.whl")):
        stem = wheel.name[: -len(".whl")]
        name, version = stem.split("-")[0], stem.split("-")[1]
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        lines.append(f"{name.replace('_', '-')}=={version} --hash=sha256:{digest}")
    LOCK.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {LOCK.name}: {len(lines)} pins")


def _verify_offline() -> None:
    """Install from the lock with no index. The only proof that matters."""
    _run(
        [
            sys.executable, "-m", "pip", "install",
            "--no-index", "--find-links", str(VENDOR),
            "--require-hashes", "--dry-run",
            "-r", str(LOCK),
        ],
        stdout=subprocess.DEVNULL,
    )
    print("verified: the lock resolves offline with --require-hashes")


def main() -> int:
    _download_closure()
    _build_from_source()
    _write_lock()
    _verify_offline()
    print(f"\nSealed {len(list(VENDOR.glob('*.whl')))} wheels into {VENDOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
