"""Keep a multi-year IM-07 deployment small, backed up and debuggable.

A production-line agent that runs once a minute writes ~525,600 runs and
telemetry rows a year. The numbers are fine for SQLite; keeping every run row,
checkpoint blob and raw log forever is not. This job runs once a day and:

1. takes a consistent backup of the platform databases first;
2. prunes finished runs older than ``--run-days`` — their steps, then the runs,
   then the LangGraph checkpoint threads those runs created (interrupted runs
   are never touched);
3. copies telemetry older than ``--telemetry-days`` into
   ``data/archive/line_telemetry_archive.db`` before deleting it from the hot
   results database;
4. removes captured demo mail older than ``--mail-days``;
5. rotates any log over ``--log-mb`` to ``.1`` (one generation);
6. keeps the newest ``--keep-backups`` backup folders.

The archive file is a normal SQLite database; a year of telemetry can be opened
with any SQLite client without touching the live one.

Run:
    python -m scripts.im07_maintenance
    python -m scripts.im07_maintenance --dry-run
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
RESULTS_DB = DATA_DIR / "results.db"
SCAFFOLD_DB = DATA_DIR / "scaffold.db"
CHECKPOINTS_DB = DATA_DIR / "checkpoints.db"
ARCHIVE_DIR = DATA_DIR / "archive"
MAILBOX_DIR = DATA_DIR / "production_line" / "mailbox"
LOG_DIR = DATA_DIR / "logs"
BACKUP_DIR = DATA_DIR / "backups"


def _connect(path: Path) -> sqlite3.Connection:
    # The backend may be writing at 03:00; wait rather than fail the whole job.
    connection = sqlite3.connect(path, timeout=30.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _backup() -> Path | None:
    """Consistent SQLite copies, with an integrity check, before any deletion."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = BACKUP_DIR / stamp
    target.mkdir(parents=True, exist_ok=False)
    copied: list[str] = []
    for name in ("scaffold.db", "checkpoints.db", "memory.db", "results.db", "quotas.db"):
        source = DATA_DIR / name
        if not source.is_file():
            continue
        destination = target / name
        try:
            with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
                with sqlite3.connect(destination) as dst:
                    src.backup(dst)
                    check = dst.execute("PRAGMA integrity_check").fetchone()
                    if not check or check[0] != "ok":
                        raise RuntimeError(f"integrity check failed for {name}: {check}")
            copied.append(name)
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the rest
            print(f"backup {name} failed: {type(exc).__name__}: {exc}")
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source": str(DATA_DIR),
                "files": copied,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def _prune_runs(days: int, dry_run: bool) -> dict[str, int]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_text = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    if not SCAFFOLD_DB.exists():
        return {"runs": 0, "steps": 0, "threads": 0}

    with _connect(SCAFFOLD_DB) as connection:
        rows = connection.execute(
            "SELECT id, thread_id FROM runs "
            "WHERE created_at < ? AND status NOT IN ('running','pending','interrupted')",
            (cutoff_text,),
        ).fetchall()
        run_ids = [str(row["id"]) for row in rows]
        thread_ids = [str(row["thread_id"]) for row in rows if row["thread_id"]]
        if dry_run or not run_ids:
            return {"runs": len(run_ids), "steps": 0, "threads": len(thread_ids)}

        steps = 0
        for start in range(0, len(run_ids), 500):
            batch = run_ids[start : start + 500]
            marks = ",".join("?" * len(batch))
            steps += connection.execute(
                f"DELETE FROM run_steps WHERE run_id IN ({marks})", batch
            ).rowcount
            connection.execute(f"DELETE FROM runs WHERE id IN ({marks})", batch)
        connection.commit()
        connection.execute("VACUUM")

    checkpoint_threads = 0
    if CHECKPOINTS_DB.exists() and thread_ids:
        with _connect(CHECKPOINTS_DB) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            for start in range(0, len(thread_ids), 400):
                batch = thread_ids[start : start + 400]
                marks = ",".join("?" * len(batch))
                if "writes" in tables:
                    connection.execute(
                        f"DELETE FROM writes WHERE thread_id IN ({marks})", batch
                    )
                if "checkpoints" in tables:
                    checkpoint_threads += connection.execute(
                        f"DELETE FROM checkpoints WHERE thread_id IN ({marks})", batch
                    ).rowcount
            connection.commit()
            if tables:
                connection.execute("VACUUM")

    return {"runs": len(run_ids), "steps": steps, "threads": checkpoint_threads}


def _archive_telemetry(days: int, dry_run: bool) -> dict[str, int]:
    if not RESULTS_DB.exists():
        return {"archived": 0}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = ARCHIVE_DIR / "line_telemetry_archive.db"

    with _connect(RESULTS_DB) as connection:
        exists = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='line_telemetry'"
        ).fetchone()
        if exists is None:
            return {"archived": 0}
        count = connection.execute(
            "SELECT COUNT(*) FROM line_telemetry WHERE _inserted_at < ?", (cutoff,)
        ).fetchone()[0]
        if dry_run or count == 0:
            return {"archived": count}

        connection.execute("ATTACH DATABASE ? AS archive", (str(archive_path),))
        connection.execute(
            "CREATE TABLE IF NOT EXISTS archive.line_telemetry AS "
            "SELECT * FROM main.line_telemetry WHERE 0"
        )
        connection.execute(
            "INSERT INTO archive.line_telemetry "
            "SELECT * FROM main.line_telemetry WHERE _inserted_at < ?",
            (cutoff,),
        )
        connection.execute(
            "DELETE FROM main.line_telemetry WHERE _inserted_at < ?", (cutoff,)
        )
        connection.commit()
        connection.execute("DETACH DATABASE archive")
        connection.execute("VACUUM")
    return {"archived": count}


def _prune_mail(days: int, dry_run: bool) -> int:
    if not MAILBOX_DIR.exists():
        return 0
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    removed = 0
    for path in MAILBOX_DIR.iterdir():
        if path.is_file() and path.stat().st_mtime < cutoff:
            removed += 1
            if not dry_run:
                path.unlink(missing_ok=True)
    return removed


def _rotate_logs(max_mb: int, dry_run: bool) -> int:
    if not LOG_DIR.exists():
        return 0
    limit = max_mb * 1024 * 1024
    rotated = 0
    for path in LOG_DIR.glob("*.log"):
        if path.stat().st_size <= limit:
            continue
        rotated += 1
        if dry_run:
            continue
        target = path.with_suffix(path.suffix + ".1")
        try:
            target.unlink(missing_ok=True)
            path.replace(target)
        except OSError as exc:
            # A live uvicorn handle cannot always be renamed on Windows; leaving
            # the log in place is better than aborting the rest of maintenance.
            print(f"could not rotate {path.name}: {exc}")
    return rotated


def _prune_backups(keep: int, dry_run: bool) -> int:
    if not BACKUP_DIR.exists() or keep < 1:
        return 0
    folders = sorted(
        (path for path in BACKUP_DIR.iterdir() if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    removed = 0
    for path in folders[keep:]:
        removed += 1
        if not dry_run:
            shutil.rmtree(path, ignore_errors=True)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-days", type=int, default=90)
    parser.add_argument("--telemetry-days", type=int, default=365)
    parser.add_argument("--mail-days", type=int, default=365)
    parser.add_argument("--log-mb", type=int, default=50)
    parser.add_argument("--keep-backups", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    print(f"maintenance start {datetime.now(timezone.utc).isoformat()}")
    backup_path = None if args.dry_run else _backup()
    if backup_path:
        print(f"backup -> {backup_path}")
    print("runs:", _prune_runs(args.run_days, args.dry_run))
    print("telemetry:", _archive_telemetry(args.telemetry_days, args.dry_run))
    print("mail removed:", _prune_mail(args.mail_days, args.dry_run))
    print("logs rotated:", _rotate_logs(args.log_mb, args.dry_run))
    print("backups removed:", _prune_backups(args.keep_backups, args.dry_run))
    print("maintenance done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
