"""A small, honest stand-in for an injection-moulding line's MES gateway.

Real lines expose this through OPC UA (EUROMAP 77) or MQTT/Sparkplug; this
process speaks HTTP/JSON so the workflow can be exercised without a PLC. The
shape is deliberately the one a gateway would present to MES:

    GET  /line/state          one production cycle: telemetry, alarms, setpoints,
                              recipe envelope and counters
    POST /line/setpoints      validated write-back with an audit record
    POST /_admin/reset        restart the demo scenario (requires the line key)

The write-back path is not a toy:

* every value is checked against the recipe envelope and the maximum allowed
  step from the current setpoint;
* changes are rate-limited;
* an unknown parameter, a non-numeric value, or a line in E-STOP is refused;
* an audit row is kept even when a change is refused.

The scenario is deterministic: the line runs normally for two cycles, then
develops a short-shot trend (melt temperature high, hold pressure low, part
weight falling). A change that raises hold pressure by at least 10 bar and melt
temperature by at least 1.5 C puts it into recovery; without a change the fault
persists.

Run:
    python -m scripts.mock_production_line
Default ports: HTTP 8101, SMTP 8025. State and captured mail stay under
``data/production_line``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import socket
import threading
import time
from datetime import datetime, timezone
from email import message_from_bytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data" / "production_line"
STATE_FILE = DATA_DIR / "im07_state.json"
MAILBOX = DATA_DIR / "mailbox"

LINE_ID = "IM-07"
RECIPE = "PP-TRAY-128"
TARGET_CYCLE_S = 31.8

ENVELOPE: dict[str, dict[str, float]] = {
    "melt_temp_c": {"min": 220.0, "max": 245.0, "max_step": 3.0},
    "hold_pressure_bar": {"min": 450.0, "max": 600.0, "max_step": 20.0},
    "injection_pressure_bar": {"min": 700.0, "max": 950.0, "max_step": 30.0},
    "cooling_time_s": {"min": 14.0, "max": 24.0, "max_step": 1.5},
}

BASE_SETPOINTS: dict[str, float] = {
    "melt_temp_c": 232.0,
    "hold_pressure_bar": 520.0,
    "injection_pressure_bar": 860.0,
    "cooling_time_s": 18.5,
}

FAULT_AFTER_CYCLES = int(os.environ.get("MOCK_FAULT_AFTER", "3"))
MIN_CHANGE_INTERVAL_S = float(os.environ.get("MOCK_MIN_CHANGE_INTERVAL", "60"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LineState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = self._load()

    def _load(self) -> dict[str, Any]:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        return self._fresh()

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {
            "line_id": LINE_ID,
            "recipe": RECIPE,
            "cycle": 0,
            "setpoints": dict(BASE_SETPOINTS),
            "phase": "normal",
            "corrected_at": None,
            "last_change_at": 0.0,
            "changes": [],
            # "short_shot" is the closed-loop scenario; "unknown_alarm" exercises
            # the escalation path where no rule proposes a change.
            "scenario": "short_shot",
        }

    def save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self.state["cycle"] += 1
            cycle = self.state["cycle"]
            setpoints = dict(self.state["setpoints"])
            phase = self.state["phase"]
            corrected_at = self.state["corrected_at"]
            # Trigger once, not every cycle: once corrected the line recovers and
            # stays normal. A recurring trigger would keep ratcheting setpoints.
            if cycle == FAULT_AFTER_CYCLES and phase == "normal":
                phase = self.state["phase"] = "fault"
            if phase == "recovering" and corrected_at is not None and cycle > corrected_at + 2:
                phase = self.state["phase"] = "normal"
            payload = self._telemetry(cycle, setpoints, phase)
            if (
                self.state.get("scenario") == "unknown_alarm"
                and phase == "normal"
                and not payload["alarms"]
            ):
                alarm = {
                    "code": "E-999",
                    "severity": "high",
                    "message": "冷却水流量低于下限，需人工检查水泵与阀门",
                }
                payload["alarms"] = [alarm]
                payload["alarm_count"] = 1
                payload["primary_alarm"] = alarm
            payload["scenario"] = self.state.get("scenario")
            self.save()
            return payload

    def _telemetry(
        self, cycle: int, setpoints: dict[str, float], phase: str
    ) -> dict[str, Any]:
        wobble = math.sin(cycle / 2.4) * 0.22
        alarms: list[dict[str, Any]] = []
        if phase == "fault":
            melt = 238.1 + wobble
            hold_actual = setpoints["hold_pressure_bar"] - 15.0
            injection_actual = setpoints["injection_pressure_bar"] - 42.0
            cycle_time = 34.6 + wobble * 0.6
            shot_weight = 126.72 + wobble * 0.05
            scrap = 4.8 + abs(wobble) * 0.2
            alarms = [
                {
                    "code": "E-311",
                    "severity": "high",
                    "message": "短射：单件重量低于下限 127.0 g，保压压力不足",
                },
                {
                    "code": "E-204",
                    "severity": "medium",
                    "message": "料筒温度高于设定值 6 C",
                },
            ]
            state = "RUNNING"
        elif phase == "recovering":
            melt = setpoints["melt_temp_c"] + 2.0 + wobble
            hold_actual = setpoints["hold_pressure_bar"] - 1.0
            injection_actual = setpoints["injection_pressure_bar"] - 6.0
            cycle_time = 32.4 + wobble * 0.3
            shot_weight = 127.65 + wobble * 0.05
            scrap = 2.1 + abs(wobble) * 0.1
            state = "RUNNING"
        else:
            melt = setpoints["melt_temp_c"] + wobble
            hold_actual = setpoints["hold_pressure_bar"] - 1.2
            injection_actual = setpoints["injection_pressure_bar"] - 4.0
            cycle_time = TARGET_CYCLE_S + wobble * 0.2
            shot_weight = 128.42 + wobble * 0.05
            scrap = 1.1 + abs(wobble) * 0.05
            state = "RUNNING"

        availability = 0.90
        performance = min(1.05, TARGET_CYCLE_S / cycle_time)
        quality = max(0.0, (100.0 - scrap) / 100.0)
        oee = availability * performance * quality * 100.0

        good = int(cycle * 2.4) if phase == "normal" else int(cycle * 2.1)
        scrap_parts = max(0, cycle * 3 - good)

        return {
            "line_id": LINE_ID,
            "timestamp": _now(),
            "cycle": cycle,
            "state": state,
            "recipe": RECIPE,
            "phase": phase,
            "telemetry": {
                "melt_temp_c": round(melt, 2),
                "mold_temp_c": round(45.0 + wobble * 0.3, 2),
                "hold_pressure_bar": round(hold_actual, 1),
                "injection_pressure_bar": round(injection_actual, 1),
                "cooling_time_s": setpoints["cooling_time_s"],
                "cycle_time_s": round(cycle_time, 2),
                "shot_weight_g": round(shot_weight, 2),
                "scrap_rate_pct": round(scrap, 2),
                "oee_pct": round(oee, 2),
            },
            "setpoints": setpoints,
            "envelope": ENVELOPE,
            "alarms": alarms,
            "alarm_count": len(alarms),
            "primary_alarm": alarms[0] if alarms else None,
            "counters": {
                "good_parts": good,
                "scrap_parts": scrap_parts,
                "target_cycle_time_s": TARGET_CYCLE_S,
            },
        }

    def apply_setpoints(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with self.lock:
            line_id = str(body.get("line_id") or "")
            if line_id != LINE_ID:
                return 404, {"error": "unknown_line", "detail": f"No line '{line_id}'."}

            changes = body.get("changes")
            if not isinstance(changes, dict) or not changes:
                return 422, {"error": "no_changes", "detail": "changes must be a non-empty object."}
            reason = str(body.get("reason") or "").strip()
            if not reason:
                return 422, {"error": "reason_required", "detail": "A change needs a reason."}

            now = time.time()
            wait = MIN_CHANGE_INTERVAL_S - (now - float(self.state.get("last_change_at") or 0))
            if wait > 0:
                return 429, {
                    "error": "rate_limited",
                    "detail": f"Next setpoint change allowed in {wait:.0f}s.",
                }

            current = dict(self.state["setpoints"])
            proposed: dict[str, float] = {}
            for key, raw in changes.items():
                if key not in ENVELOPE:
                    return 422, {"error": "unknown_parameter", "detail": f"'{key}' is not writable."}
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    return 422, {"error": "not_numeric", "detail": f"'{key}' must be numeric."}
                limit = ENVELOPE[key]
                if not limit["min"] <= value <= limit["max"]:
                    return 409, {
                        "error": "outside_recipe_envelope",
                        "detail": f"{key}={value} outside [{limit['min']}, {limit['max']}].",
                    }
                if abs(value - current[key]) > limit["max_step"] + 1e-9:
                    return 409, {
                        "error": "step_too_large",
                        "detail": (
                            f"{key} step {abs(value-current[key]):.2f} exceeds "
                            f"{limit['max_step']}."
                        ),
                    }
                proposed[key] = value

            before = dict(current)
            current.update(proposed)
            self.state["setpoints"] = current
            self.state["last_change_at"] = now
            if (
                proposed.get("hold_pressure_bar", 0) >= BASE_SETPOINTS["hold_pressure_bar"] + 10
                and proposed.get("melt_temp_c", 0) >= BASE_SETPOINTS["melt_temp_c"] + 1.5
            ):
                self.state["phase"] = "recovering"
                self.state["corrected_at"] = self.state["cycle"]

            audit = {
                "at": _now(),
                "cycle": self.state["cycle"],
                "actor": str(body.get("actor") or "unknown"),
                "run_id": str(body.get("run_id") or ""),
                "reason": reason,
                "before": before,
                "after": dict(current),
                "changes": proposed,
            }
            self.state.setdefault("changes", []).append(audit)
            self.state["changes"] = self.state["changes"][-200:]
            self.save()
            return 200, {
                "ok": True,
                "line_id": LINE_ID,
                "setpoints": current,
                "audit": audit,
                "phase": self.state["phase"],
            }

    def reset(self) -> dict[str, Any]:
        with self.lock:
            self.state = self._fresh()
            self.save()
            return {"ok": True, "setpoints": self.state["setpoints"]}

    def set_scenario(self, name: str) -> tuple[int, dict[str, Any]]:
        allowed = {"short_shot", "unknown_alarm"}
        if name not in allowed:
            return 422, {"error": "unknown_scenario", "allowed": sorted(allowed)}
        with self.lock:
            self.state["scenario"] = name
            self.save()
            return 200, {"ok": True, "scenario": name}


class MailSink(threading.Thread):
    """Capture SMTP messages to files; enough for smtplib's plain handshake."""

    daemon = True

    def __init__(self, host: str, port: int) -> None:
        super().__init__(name="mock-smtp")
        self.socket = socket.socket()
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind((host, port))
        self.socket.listen(8)
        self.stopping = False

    def run(self) -> None:
        MAILBOX.mkdir(parents=True, exist_ok=True)
        while not self.stopping:
            try:
                conn, _ = self.socket.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            stream = conn.makefile("rwb")
            stream.write(b"220 im07-smtp ESMTP\r\n")
            stream.flush()
            data_mode = False
            chunks: list[bytes] = []
            while True:
                line = stream.readline()
                if not line:
                    return
                if data_mode:
                    if line in (b".\r\n", b".\n"):
                        raw = b"".join(chunks)
                        MAILBOX.mkdir(parents=True, exist_ok=True)
                        name = f"mail-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}.eml"
                        (MAILBOX / name).write_bytes(raw)
                        message = message_from_bytes(raw)
                        (MAILBOX / f"{name}.json").write_text(
                            json.dumps(
                                {
                                    "subject": message.get("Subject", ""),
                                    "to": message.get("To", ""),
                                    "from": message.get("From", ""),
                                    "body": message.get_payload(decode=True).decode(
                                        "utf-8", "replace"
                                    )
                                    if message.is_multipart() is False
                                    else "",
                                },
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                        data_mode = False
                        stream.write(b"250 queued\r\n")
                        stream.flush()
                        continue
                    chunks.append(line)
                    continue
                verb = line.strip().upper()
                if verb.startswith((b"EHLO", b"HELO")):
                    stream.write(b"250-im07\r\n250 OK\r\n")
                elif verb.startswith((b"MAIL FROM", b"RCPT TO")):
                    stream.write(b"250 OK\r\n")
                elif verb == b"DATA":
                    data_mode = True
                    chunks = []
                    stream.write(b"354 send it\r\n")
                elif verb == b"QUIT":
                    stream.write(b"221 bye\r\n")
                    return
                else:
                    stream.write(b"250 OK\r\n")
                stream.flush()


class Handler(BaseHTTPRequestHandler):
    state: LineState
    api_key: str

    def log_message(self, *_args: Any) -> None:
        return None

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _authorised(self) -> bool:
        return self.headers.get("X-Line-Key", "") == self.api_key

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"status": "ok", "line_id": LINE_ID})
        elif path == "/line/state":
            if not self._authorised():
                self._send(401, {"error": "unauthorised"})
                return
            self._send(200, self.state.snapshot())
        elif path == "/line/config":
            if not self._authorised():
                self._send(401, {"error": "unauthorised"})
                return
            with self.state.lock:
                self._send(
                    200,
                    {
                        "line_id": LINE_ID,
                        "setpoints": dict(self.state.state["setpoints"]),
                        "envelope": ENVELOPE,
                        "changes": self.state.state.get("changes", [])[-20:],
                    },
                )
        else:
            self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = self.path.split("?", 1)[0]
        if path == "/line/setpoints":
            if not self._authorised():
                self._send(401, {"error": "unauthorised"})
                return
            status, payload = self.state.apply_setpoints(self._body())
            self._send(status, payload)
        elif path == "/_admin/reset":
            if not self._authorised():
                self._send(401, {"error": "unauthorised"})
                return
            self._send(200, self.state.reset())
        elif path == "/_admin/scenario":
            if not self._authorised():
                self._send(401, {"error": "unauthorised"})
                return
            status, payload = self.state.set_scenario(str(self._body().get("name") or ""))
            self._send(status, payload)
        else:
            self._send(404, {"error": "not_found"})


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock injection-moulding line gateway")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOCK_LINE_PORT", "8101")))
    parser.add_argument(
        "--smtp-port", type=int, default=int(os.environ.get("MOCK_SMTP_PORT", "8025"))
    )
    parser.add_argument("--api-key", default=os.environ.get("LINE_API_KEY", "demo-line-key"))
    args = parser.parse_args()

    Handler.state = LineState()
    Handler.api_key = args.api_key
    smtp = MailSink(args.host, args.smtp_port)
    smtp.start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(
        f"Mock line {LINE_ID} on http://{args.host}:{args.port} "
        f"(SMTP {args.smtp_port}, state {STATE_FILE})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        smtp.stopping = True
        smtp.socket.close()


if __name__ == "__main__":
    main()
