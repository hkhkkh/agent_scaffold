"""Runtime settings.

Lets the LLM endpoint be changed from the UI without editing ``.env`` and
restarting — the common case being "I want to point this at a different model".

Only the LLM connection is editable at runtime. Operational settings
(``RECURSION_LIMIT``, ``CODE_NODE_ENABLED``, CORS) stay environment-only on
purpose: a setting that changes how much a run may spend, or whether arbitrary
code executes, should require a deliberate file edit rather than a stray click.
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.runtime.ai.provider import resolve_llm_config
from app.security.policy import require_admin
from app.security.secrets import SECRET_LLM_API_KEY, get_secret_provider
from app.settings import get_settings

router = APIRouter(
    prefix="/settings",
    tags=["settings"],
    dependencies=[Depends(require_admin)],
)


class LLMSettings(BaseModel):
    base_url: str = Field(description="OpenAI-compatible endpoint.")
    model: str
    api_key: str = ""
    temperature: float = 0.0


class SettingsResponse(BaseModel):
    llm: LLMSettings
    #: True when no key is configured and the backend is using the mock model.
    mock_mode: bool
    recursion_limit: int
    code_node_enabled: bool
    ai_enabled: bool


def _current() -> SettingsResponse:
    settings = get_settings()
    from app.runtime.ai.provider import is_mock_configured

    return SettingsResponse(
        llm=LLMSettings(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            # Never return a real key; the client only needs to know one exists.
            # Read through the provider so a key from a secrets file counts as
            # present — the UI would otherwise show "not configured" while the
            # backend was happily using one.
            api_key="***"
            if get_secret_provider().has(SECRET_LLM_API_KEY)
            else "",
            temperature=settings.llm_temperature,
        ),
        mock_mode=is_mock_configured(settings),
        recursion_limit=settings.recursion_limit,
        code_node_enabled=settings.code_node_enabled,
        ai_enabled=settings.ai_enabled,
    )


@router.get("", summary="Current runtime settings")
def show() -> SettingsResponse:
    return _current()


@router.put("/llm", summary="Override the LLM connection for this process")
def update_llm(payload: LLMSettings) -> SettingsResponse:
    """Apply an LLM override in memory.

    Deliberately *not* written back to ``.env``: persisting a credential from an
    HTTP request would silently rot the config file, and the file is the source
    of truth on restart.
    """
    settings = get_settings()
    provider = get_secret_provider()
    settings.llm_base_url = payload.base_url.strip()
    settings.llm_model = payload.model.strip()
    if payload.api_key.strip() and payload.api_key.strip() != "***":
        # A secrets file wins over `.env` (that is what lets an exported agent
        # carry its own credentials), so writing the in-memory setting would be
        # a silent no-op: the UI would report success and the model would keep
        # using the old key. Refusing is the only honest answer.
        if provider.source != "settings":
            raise HTTPException(
                status_code=409,
                detail=(
                    "This deployment reads credentials from a secrets source "
                    f"('{provider.source}'), so an in-memory override would have "
                    "no effect. Edit that source instead."
                ),
            )
        settings.llm_api_key = payload.api_key.strip()
    settings.llm_temperature = payload.temperature
    return _current()


@router.get("/llm/models", summary="List models the endpoint advertises")
async def list_models() -> dict[str, Any]:
    """Ask the endpoint for its model list.

    Best-effort: Ollama, vLLM, and OpenAI all expose ``/models``, but a
    provider that does not is not an error — the UI falls back to free text.
    """
    settings = get_settings()
    config = resolve_llm_config(None, settings)
    url = f"{config['base_url'].rstrip('/')}/models"

    headers = {}
    if config["api_key"]:
        headers["Authorization"] = f"Bearer {config['api_key']}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The endpoint returned {exc.response.status_code} for {url}.",
        ) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"Could not reach {url}: {exc}"
        ) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    models = [
        str(item.get("id"))
        for item in (data or [])
        if isinstance(item, dict) and item.get("id")
    ]
    return {"models": sorted(models), "current": settings.llm_model}


@router.post("/llm/test", summary="Test the LLM connection")
async def test_connection() -> dict[str, Any]:
    """Send a minimal request and report exactly what happened.

    Worth having as a dedicated endpoint: "the AI features are not working" is
    almost always a wrong base URL, a wrong model name, or an exhausted balance,
    and each of those produces a specific error worth surfacing verbatim.
    """
    settings = get_settings()
    config = resolve_llm_config(None, settings)

    if not config["api_key"]:
        return {
            "ok": False,
            "mock_mode": True,
            "detail": (
                "No API key is configured, so the backend is running in mock mode. "
                "Add LLM_API_KEY to .env to use a real model."
            ),
        }

    url = f"{config['base_url'].rstrip('/')}/chat/completions"
    body = {
        "model": config["model"],
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_tokens": 12,
        "temperature": 0,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {config['api_key']}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"Could not reach {url}: {exc}"}

    if response.status_code >= 400:
        return {
            "ok": False,
            "status": response.status_code,
            "detail": response.text[:800],
            "hint": _hint_for(response.status_code),
        }

    try:
        payload = response.json()
    except ValueError:
        return {"ok": False, "detail": "The endpoint returned a non-JSON response."}

    choice = (payload.get("choices") or [{}])[0]
    return {
        "ok": True,
        "model": payload.get("model"),
        "reply": (choice.get("message") or {}).get("content", "")[:200],
        "usage": payload.get("usage"),
    }


def _hint_for(status_code: int) -> str:
    return {
        401: "The API key was rejected. Check LLM_API_KEY in .env.",
        402: "The account has insufficient balance.",
        404: "The endpoint or model name is wrong. Check LLM_BASE_URL and LLM_MODEL.",
        429: "Rate limited. Wait a moment, or use a different model.",
    }.get(status_code, "See the provider's message above.")


__all__ = ["router"]
