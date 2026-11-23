"""Verify the configured LLM endpoint is reachable and the model exists.

Usage
-----
    d:\\AI_LLM_Project\\venv_d\\Scripts\\python.exe scripts/check_llm.py

Reads the same `.env` the backend uses, so a green result here means the
backend will work too. Uses only the standard library plus the project
settings, so it runs before dependencies are installed if you pass --raw.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def load_config() -> dict[str, str]:
    try:
        from app.settings import get_settings

        settings = get_settings()
        return {
            "base_url": settings.llm_base_url,
            "api_key": settings.llm_api_key,
            "model": settings.llm_model,
        }
    except ImportError:
        # Fallback: parse .env by hand so this script works before pip install.
        env_path = Path(__file__).resolve().parents[1] / ".env"
        values: dict[str, str] = {}
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip().lower()] = value.strip()
        return {
            "base_url": values.get("llm_base_url", "https://api.openai.com/v1"),
            "api_key": values.get("llm_api_key", ""),
            "model": values.get("llm_model", "gpt-4o-mini"),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check LLM endpoint connectivity.")
    parser.add_argument("--model", help="Override the model name to test.")
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    config = load_config()
    base_url = config["base_url"].rstrip("/")
    model = args.model or config["model"]
    api_key = config["api_key"]

    if not api_key:
        print("No LLM_API_KEY set -> the backend will run in MOCK mode.")
        print("Set LLM_API_KEY in .env to use a real provider.")
        return 0

    print(f"endpoint : {base_url}")
    print(f"model    : {model}")
    print(f"api key  : {api_key[:7]}...{api_key[-4:]}  ({len(api_key)} chars)")
    print("-" * 60)

    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
            "max_tokens": 12,
            "temperature": 0,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"FAILED  HTTP {exc.code}", file=sys.stderr)
        print(detail[:1200], file=sys.stderr)
        if exc.code == 401:
            print("\n-> The API key was rejected.", file=sys.stderr)
        elif exc.code == 404:
            print(f"\n-> Endpoint or model not found. Try --model <name>.", file=sys.stderr)
        elif exc.code == 402:
            print("\n-> Insufficient balance for this account.", file=sys.stderr)
        return 1
    except urllib.error.URLError as exc:
        print(f"FAILED  cannot reach {base_url}: {exc.reason}", file=sys.stderr)
        return 1

    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "")
    usage = body.get("usage") or {}

    print("SUCCESS")
    print(f"  replied  : {content.strip()[:120]!r}")
    print(f"  model    : {body.get('model')}")
    if usage:
        print(f"  tokens   : {usage.get('total_tokens')} total")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
