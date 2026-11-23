"""Workspace files: the bytes a conversation works on.

Why an endpoint exists at all
----------------------------
The files capability pack's tools operate on an agent workspace, and a user has
to be able to put something there. Without an upload path the pack is reachable
only from a test; with it, "upload the spreadsheet, then ask about it" is two
requests.

The workspace is the boundary, not the process: every path goes through
`Workspace.resolve`, which refuses absolute paths, `..`, and symlink escapes.
The API writes through the same object the tools read through, so the two cannot
disagree about what "the workspace" is.

Disabled in the public demo
---------------------------
Writing a visitor's bytes to disk is exactly what an anonymous shared deployment
must not do. The refusal is explicit here rather than inherited from a middleware,
so the error says why instead of surfacing as a mysterious 404.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.runtime.audit import AuditError, record_audit
from app.runtime.permissions import (
    FILES_LIST,
    FILES_READ,
    FILES_WRITE,
    PermissionDenied,
    check_permission,
)
from app.runtime.workspace import Workspace, WorkspaceError, workspace_from_env
from app.settings import get_settings

router = APIRouter(prefix="/files", tags=["files"])

#: A JSON body is not a great upload channel, but this deployment deliberately
#: does not depend on `python-multipart` (removed because nothing used it, and it
#: is a CVE surface). Base64 costs a third more bytes and needs no new dependency.
MAX_INLINE_BYTES = 64 * 1024 * 1024


class FileUpload(BaseModel):
    name: str = Field(..., description="Path inside the workspace, e.g. `orders.xlsx`.")
    content_base64: str = Field("", description="Base64-encoded bytes.")
    content_text: str | None = Field(
        None, description="UTF-8 text, for the cases where base64 is inconvenient."
    )


class FileWriteResult(BaseModel):
    name: str
    size: int


def _workspace() -> Workspace:
    settings = get_settings()
    if settings.public_demo:
        raise HTTPException(
            status_code=403,
            detail="File uploads are disabled in the public demo.",
        )
    return workspace_from_env()


def _permission(permission: str, *, tool: str) -> None:
    """Turn a policy refusal into an HTTP answer that names the reason."""
    try:
        check_permission(permission, tool=tool)
    except PermissionDenied as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def _path_error(exc: WorkspaceError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("")
def list_files() -> dict[str, Any]:
    """Every file currently in the workspace, newest last."""
    workspace = _workspace()
    _permission(FILES_LIST, tool="api.files.list")
    return {"files": workspace.list()}


@router.post("", response_model=FileWriteResult, status_code=201)
def upload_file(payload: FileUpload) -> FileWriteResult:
    workspace = _workspace()
    _permission(FILES_WRITE, tool="api.files.upload")
    if payload.content_base64:
        try:
            data = base64.b64decode(payload.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"content_base64 is not valid base64: {exc}"
            ) from exc
    elif payload.content_text is not None:
        data = payload.content_text.encode("utf-8")
    else:
        raise HTTPException(
            status_code=400, detail="Provide content_base64 or content_text."
        )
    if len(data) > MAX_INLINE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"refusing {len(data)} bytes; the inline limit is {MAX_INLINE_BYTES}",
        )
    try:
        workspace.ensure_capacity()
        # Audited before the write: if the audit trail cannot be written, the
        # upload does not happen. Same rule the files pack's write tool follows.
        record_audit(
            "file.upload",
            actor="api",
            tool="api.files.upload",
            target=payload.name,
            detail={"size": len(data)},
        )
        result = workspace.write_bytes(payload.name, data)
    except WorkspaceError as exc:
        raise _path_error(exc) from exc
    except AuditError as exc:
        raise HTTPException(status_code=500, detail=f"audit unavailable: {exc}") from exc
    return FileWriteResult(**result)


@router.get("/{name:path}")
def download_file(name: str) -> Response:
    workspace = _workspace()
    _permission(FILES_READ, tool="api.files.download")
    try:
        data = workspace.read_bytes(name)
    except WorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    filename = name.rsplit("/", 1)[-1]
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.delete("/{name:path}")
def delete_file(name: str) -> dict[str, Any]:
    workspace = _workspace()
    _permission(FILES_WRITE, tool="api.files.delete")
    try:
        record_audit(
            "file.delete", actor="api", tool="api.files.delete", target=name
        )
        return workspace.delete(name)
    except WorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
