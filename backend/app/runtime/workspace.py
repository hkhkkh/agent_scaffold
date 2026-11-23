"""A bounded directory that capability packs may read and write.

Why this is not just `Path`

The platform already has a sandbox for *code*, but the files capability needs the
opposite trade-off: it must open files, and it must be able to say "no" before it
does. Two failure modes make a raw path unsafe:

1. **Traversal.** `../../.env` is a valid relative path. Every call therefore
   resolves against the workspace root and then checks containment.
2. **Symlink escape.** A path can be inside the workspace textually and outside
   it physically. ``resolve()`` follows links, and containment is checked on the
   result.

The bounds (bytes per file, files per workspace) are not security theatre: a
conversation that uploads a 2 GB CSV should get a clear refusal instead of eating
the disk. They are deliberately generous defaults, and they are settable per
deployment.

Where the root is
-----------------
* Platform: ``<DATA_DIR>/workspaces/<id>``. One directory per conversation when
  a session provides an id, else ``default``.
* Exported agent: ``AGENT_WORKSPACE_DIR`` or ``<agent package>/workspace``.

Both are computed by :func:`resolve_workspace_root`, so a tool never guesses.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_FILES",
    "DEFAULT_MAX_READ_BYTES",
    "Workspace",
    "WorkspaceError",
    "resolve_workspace_root",
    "workspace_from_env",
]

#: Refuse to read more than this in one call. Bigger reads are almost always a
#: mistake on the caller's side; the error names the limit so it is fixable.
DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024

#: Refuse to create a file bigger than this without an explicit deployment
#: override. xlsx files with 100k rows are about this size.
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024

#: Cap the number of files so an agent cannot turn a chat into a disk-filling
#: loop. Uploads replace, so the practical ceiling is much lower.
DEFAULT_MAX_FILES = 2_000

#: Windows rejects these names regardless of directory. Refusing them here gives
#: the model an actionable message instead of an OSError with a winerror code.
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_FORBIDDEN_NAME_CHARS = set('<>:"|?*')


class WorkspaceError(ValueError):
    """A path or operation the workspace refuses.

    ``ValueError`` rather than ``SecurityError`` because most of these are
    caller mistakes (absolute path, too large, reserved name); the containment
    failures are the security-relevant subset, and they are still refusals.
    """


@dataclass
class Workspace:
    """A directory with containment and size limits."""

    root: Path
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES
    max_write_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_files: int = DEFAULT_MAX_FILES

    def __post_init__(self) -> None:
        self.root = Path(self.root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()

    # ------------------------------------------------------------------ paths
    def resolve(self, relative: str) -> Path:
        """Turn a caller-supplied name into a path inside the workspace.

        Refuses absolutes, drive-qualified paths, `..` components and any path
        whose *resolved* location is outside the root. The last check is the one
        that catches a symlink sitting inside the workspace.
        """
        name = (relative or "").strip()
        if not name:
            raise WorkspaceError("file name is empty")
        if "\x00" in name:
            raise WorkspaceError("file name contains a NUL byte")
        pure = Path(name)
        if pure.is_absolute() or pure.drive:
            raise WorkspaceError(
                f"'{name}' is an absolute path; workspace files are named relative to the workspace"
            )
        parts = [part for part in pure.parts if part not in ("", ".")]
        if any(part == ".." for part in parts):
            raise WorkspaceError(f"'{name}' escapes the workspace ('..' is not allowed)")
        for part in parts:
            self._check_component(part)
        candidate = self.root.joinpath(*parts)
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:  # pragma: no cover - resolve() is platform specific
            raise WorkspaceError(
                f"'{name}' resolves outside the workspace"
            ) from exc
        return resolved

    @staticmethod
    def _check_component(part: str) -> None:
        if part in (".", ".."):
            raise WorkspaceError(f"'{part}' is not allowed in a workspace path")
        if set(part) & _FORBIDDEN_NAME_CHARS:
            raise WorkspaceError(
                f"'{part}' contains a character Windows forbids: {''.join(sorted(_FORBIDDEN_NAME_CHARS))}"
            )
        if part[-1:] in (" ", "."):
            raise WorkspaceError(f"'{part}' ends with a space or dot, which Windows drops")
        stem = part.split(".", 1)[0].upper()
        if stem in _RESERVED_WINDOWS_NAMES:
            raise WorkspaceError(f"'{part}' is a reserved Windows device name")

    def relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.root).as_posix()

    # ----------------------------------------------------------------- listing
    def list(self, pattern: str = "*", *, recursive: bool = True) -> list[dict[str, Any]]:
        """Files in the workspace, sorted by name, as plain serialisable dicts.

        Recursive by default: a model asked "what files do I have?" needs to see
        an upload the HTTP layer put in a sub-directory, and a flat listing that
        silently omits it looks like a failed upload.
        """
        matches = self.root.rglob(pattern) if recursive else self.root.glob(pattern)
        entries: list[dict[str, Any]] = []
        for path in sorted(matches):
            if path.is_dir() or path.name.startswith("."):
                continue
            try:
                stat = path.stat()
            except OSError:  # a file that vanished between glob and stat
                continue
            entries.append(
                {
                    "name": self.relative(path),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
            if len(entries) >= self.max_files:
                break
        return entries

    # ----------------------------------------------------------------- reading
    def read_bytes(self, name: str, *, limit: int | None = None) -> bytes:
        path = self.resolve(name)
        if not path.is_file():
            raise WorkspaceError(f"'{name}' does not exist in the workspace")
        size = path.stat().st_size
        allowed = self.max_read_bytes if limit is None else min(limit, self.max_read_bytes)
        if size > allowed:
            raise WorkspaceError(
                f"'{name}' is {size} bytes; this deployment reads at most {allowed} bytes per call"
            )
        return path.read_bytes()

    def read_text(self, name: str, *, limit: int | None = None) -> str:
        raw = self.read_bytes(name, limit=limit)
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise WorkspaceError(
            f"'{name}' is not UTF-8 or GB18030 text; read it as binary or convert it first"
        )

    # ----------------------------------------------------------------- writing
    def write_bytes(self, name: str, data: bytes, *, overwrite: bool = True) -> dict[str, Any]:
        if len(data) > self.max_write_bytes:
            raise WorkspaceError(
                f"refusing to write {len(data)} bytes; this deployment allows at most "
                f"{self.max_write_bytes} bytes per file"
            )
        path = self.resolve(name)
        if path.exists() and not overwrite:
            raise WorkspaceError(f"'{name}' already exists and overwrite is off")
        if path.is_dir():
            raise WorkspaceError(f"'{name}' is a directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic on every platform the runtime supports: a reader never sees a
        # half-written spreadsheet, and a crash does not leave a truncated file
        # that looks valid.
        handle, tmp_name = tempfile.mkstemp(prefix=".write-", dir=str(path.parent))
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(data)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return {"name": self.relative(path), "size": len(data)}

    def write_text(self, name: str, text: str, *, overwrite: bool = True) -> dict[str, Any]:
        return self.write_bytes(name, text.encode("utf-8"), overwrite=overwrite)

    # ------------------------------------------------------------- lifecycle
    def delete(self, name: str) -> dict[str, Any]:
        path = self.resolve(name)
        if not path.is_file():
            raise WorkspaceError(f"'{name}' does not exist in the workspace")
        path.unlink()
        return {"name": self.relative(path), "deleted": True}

    def file_count(self) -> int:
        return sum(1 for path in self.root.rglob("*") if path.is_file())

    def ensure_capacity(self, adding: int = 1) -> None:
        if self.file_count() + adding > self.max_files:
            raise WorkspaceError(
                f"this workspace already holds {self.file_count()} files; "
                f"the limit is {self.max_files}"
            )

    def clear(self) -> None:
        """Remove everything inside the workspace (the root itself stays)."""
        for child in self.root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()


def resolve_workspace_root(
    settings: Any = None,
    *,
    workspace_id: str = "",
    data_dir: Path | str | None = None,
) -> Path:
    """Where this process's workspace lives.

    Resolution order is explicit on purpose, so a deployment can move it without
    touching code:

    * ``AGENT_WORKSPACE_DIR`` (an exported agent sets this to ``<package>/workspace``)
    * ``WORKSPACE_DIR``
    * ``<DATA_DIR>/workspaces/<workspace_id or 'default'>``
    """
    override = os.environ.get("AGENT_WORKSPACE_DIR") or os.environ.get("WORKSPACE_DIR")
    if override:
        return Path(override)
    if data_dir is None:
        if settings is not None:
            data_dir = settings.data_dir
        else:
            env_dir = os.environ.get("DATA_DIR")
            data_dir = Path(env_dir) if env_dir else Path.cwd() / "data"
    safe_id = "".join(ch for ch in (workspace_id or "default") if ch.isalnum() or ch in "-_")
    return Path(data_dir) / "workspaces" / (safe_id or "default")


def workspace_from_env(*, workspace_id: str = "") -> Workspace:
    """The workspace a tool call should use, built from the environment only.

    Tools are plain callables with no run context, so the root has to come from
    process configuration. This keeps a tool callable from the canvas, from a
    test and from an exported agent without a second code path.
    """
    return Workspace(resolve_workspace_root(workspace_id=workspace_id))
