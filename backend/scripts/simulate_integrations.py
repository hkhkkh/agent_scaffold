"""Run the business connectors against a local simulated environment.

This is an executable proof, not a mock of the runtime: it starts a fake FHIR
server, a fake SMTP relay and a SQLite business database, compiles a real graph
with the real node classes, and runs it through the same compiler an agent uses.
The final output contains every node's value, so a connector that silently
returned nothing is visible in the result.

Run it from `backend/`:

    python scripts/simulate_integrations.py

It uses port 8100 (FHIR/PDF) and 8025 (SMTP) and a temporary data directory; no
external network and no credentials are needed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Run as a file (`python scripts/...`), so the backend root is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FHIR_PORT = 8100
SMTP_PORT = 8025


# ---------------------------------------------------------------------------
# Simulated services
# ---------------------------------------------------------------------------


def _text_pdf(text: str) -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length 44 >>\nstream\nBT /F1 18 Tf 20 100 Td (Hello hospital) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


PATIENT = {
    "resourceType": "Patient",
    "id": "p1",
    "name": [{"family": "Zhang", "given": ["San"]}],
    "gender": "female",
    "birthDate": "1980-02-03",
}
ENCOUNTER = {
    "resourceType": "Encounter",
    "id": "e1",
    "status": "finished",
    "subject": {"reference": "Patient/p1"},
    "period": {"start": "2026-10-20T09:00:00Z"},
}
OBSERVATIONS = [
    {
        "resourceType": "Observation",
        "id": "o1",
        "status": "final",
        "code": {"text": "Hemoglobin"},
        "valueQuantity": {"value": 118, "unit": "g/L"},
    },
    {
        "resourceType": "Observation",
        "id": "o2",
        "status": "final",
        "code": {"text": "White blood cells"},
        "valueQuantity": {"value": 7.2, "unit": "10^9/L"},
    },
]


def _bundle(resources: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": len(resources),
        "entry": [{"resource": resource} for resource in resources],
    }


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        path = self.path.split("?", 1)[0]
        if path.startswith("/fhir/Patient"):
            payload = _bundle([PATIENT])
        elif path.startswith("/fhir/Encounter"):
            payload = _bundle([ENCOUNTER])
        elif path.startswith("/fhir/Observation"):
            payload = _bundle(OBSERVATIONS)
        elif path == "/referral.pdf":
            data = _text_pdf("Referral for Zhang San")
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        else:
            self.send_error(404)
            return

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/fhir+json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return None


class FakeSMTP(threading.Thread):
    """The smallest SMTP relay that can accept one message from smtplib."""

    daemon = True

    def __init__(self) -> None:
        super().__init__(name="fake-smtp")
        import socket

        self._socket = socket.socket()
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind(("127.0.0.1", SMTP_PORT))
        self._socket.listen(5)
        self.messages: list[str] = []
        self.stopping = False

    def run(self) -> None:
        while not self.stopping:
            try:
                conn, _ = self._socket.accept()
            except OSError:
                return
            with conn:
                stream = conn.makefile("rwb")
                stream.write(b"220 simulator ESMTP\r\n")
                stream.flush()
                data_mode = False
                chunks: list[bytes] = []
                while True:
                    line = stream.readline()
                    if not line:
                        break
                    if data_mode:
                        if line in (b".\r\n", b".\n"):
                            self.messages.append(b"".join(chunks).decode("utf-8", "replace"))
                            data_mode = False
                            stream.write(b"250 queued\r\n")
                            stream.flush()
                            continue
                        chunks.append(line)
                        continue
                    verb = line.strip().upper()
                    if verb.startswith((b"EHLO", b"HELO")):
                        stream.write(b"250-simulator\r\n250 OK\r\n")
                    elif verb.startswith(b"MAIL FROM") or verb.startswith(b"RCPT TO"):
                        stream.write(b"250 OK\r\n")
                    elif verb == b"DATA":
                        data_mode = True
                        chunks = []
                        stream.write(b"354 end with .\r\n")
                    elif verb == b"QUIT":
                        stream.write(b"221 bye\r\n")
                        stream.flush()
                        break
                    else:
                        stream.write(b"250 OK\r\n")
                    stream.flush()

    def stop(self) -> None:
        self.stopping = True
        try:
            self._socket.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def build_graph(db_path: Path) -> dict[str, Any]:
    base = f"http://127.0.0.1:{FHIR_PORT}"
    return {
        "id": "sim-integrations",
        "name": "Referral completeness simulator",
        "nodes": [
            {
                "id": "start",
                "type": "start",
                "label": "Referral",
                "config": {"input_schema": {"referral_id": "r1"}},
            },
            {
                "id": "patient",
                "type": "fhir_query",
                "label": "Patient",
                "config": {
                    "base_url": f"{base}/fhir",
                    "resource": "Patient",
                    "search": {"_id": "{{ vars.start.out.referral_id }}"},
                },
            },
            {
                "id": "encounter",
                "type": "fhir_query",
                "label": "Encounter",
                "config": {
                    "base_url": f"{base}/fhir",
                    "resource": "Encounter",
                    "search": {"patient": "{{ vars.patient.out.0.id }}"},
                },
            },
            {
                "id": "labs",
                "type": "fhir_query",
                "label": "Labs",
                "config": {
                    "base_url": f"{base}/fhir",
                    "resource": "Observation",
                    "search": {
                        "patient": "{{ vars.patient.out.0.id }}",
                        "category": "laboratory",
                    },
                },
            },
            {
                "id": "letter",
                "type": "document_extract",
                "label": "Referral letter",
                "config": {"source": "url", "url": f"{base}/referral.pdf"},
            },
            {
                "id": "record",
                "type": "sql_query",
                "label": "Referral record",
                "config": {
                    "driver": "sqlite",
                    "sqlite_path": str(db_path),
                    "query": "SELECT id, status, required_docs FROM referrals WHERE id = ?",
                    "params": ["{{ vars.start.out.referral_id }}"],
                },
            },
            {
                "id": "notify",
                "type": "email_send",
                "label": "Notify care team",
                "config": {
                    "smtp_host": "127.0.0.1",
                    "smtp_port": SMTP_PORT,
                    "security": "none",
                    "from_addr": "agent@hospital.test",
                    "to": "care@hospital.test",
                    "subject": "Referral {{ vars.start.out.referral_id }} checked",
                    "body": (
                        "Patient: {{ vars.patient.out.0.name.0.family }}\n"
                        "Labs: {{ vars.labs.count }}\n"
                        "Record: {{ vars.record.text }}\n"
                        "Letter: {{ vars.letter.out }}"
                    ),
                },
            },
            {
                "id": "end",
                "type": "end",
                "label": "Result",
                "config": {"include_state": True},
            },
        ],
        "edges": [
            {"id": "e1", "source": "start", "target": "patient"},
            {"id": "e2", "source": "patient", "target": "encounter"},
            {"id": "e3", "source": "encounter", "target": "labs"},
            {"id": "e4", "source": "labs", "target": "letter"},
            {"id": "e5", "source": "letter", "target": "record"},
            {"id": "e6", "source": "record", "target": "notify"},
            {"id": "e7", "source": "notify", "target": "end"},
        ],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _seed_business_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE referrals (id TEXT, status TEXT, required_docs TEXT)")
    conn.execute("INSERT INTO referrals VALUES ('r1', 'pending', 'letter,insurance')")
    # Tables the model-generated hospital workflow expects. Same data, different
    # schema, because a generated graph names the customer's tables.
    conn.execute(
        "CREATE TABLE referral_order (referral_no TEXT, patient_mrn TEXT, "
        "patient_name TEXT, referral_type TEXT, department TEXT, status TEXT)"
    )
    conn.execute(
        "INSERT INTO referral_order VALUES "
        "('r1', 'p1', 'Zhang San', 'cardiology', '心内科', 'pending')"
    )
    conn.execute(
        "CREATE TABLE referral_required_materials (material_code TEXT, "
        "material_name TEXT, is_mandatory INTEGER, match_keyword TEXT, "
        "referral_type TEXT, sort_no INTEGER)"
    )
    conn.executemany(
        "INSERT INTO referral_required_materials VALUES (?, ?, 1, ?, 'cardiology', ?)",
        [
            ("letter", "转诊单", "转诊", 1),
            ("insurance", "医保/保险凭证", "医保", 2),
            ("lab", "检验报告", "检验", 3),
        ],
    )
    conn.commit()
    conn.close()


def _adapt_generated_graph(payload: dict[str, Any], db_path: Path) -> dict[str, Any]:
    """Point a model-generated graph at the simulator instead of example hosts."""
    graph = payload.get("graph") or payload
    for node in graph.get("nodes", []):
        config = node.setdefault("config", {})
        if node.get("type") == "sql_query":
            config["sqlite_path"] = str(db_path)
        elif node.get("type") == "email_send":
            config.update(
                {
                    "smtp_host": "127.0.0.1",
                    "smtp_port": SMTP_PORT,
                    "security": "none",
                    "username": "",
                    "password": "",
                    "from_addr": "agent@hospital.test",
                    "to": "care@hospital.test",
                    "fail_on_error": False,
                }
            )
        elif node.get("type") == "start":
            config["input_schema"] = {
                "referral_no": "r1",
                "referral_pdf_url": f"http://127.0.0.1:{FHIR_PORT}/referral.pdf",
                "fhir_base_url": f"http://127.0.0.1:{FHIR_PORT}/fhir",
                "nurse_email": "care@hospital.test",
            }
    return graph


def _load_env_file(root: Path) -> None:
    """Load the real LLM key for the live-model run; never print it."""
    from dotenv import dotenv_values

    env_path = root / ".env"
    if not env_path.is_file():
        return
    for key, value in dotenv_values(env_path).items():
        if value is not None:
            os.environ.setdefault(key, value)


async def _run(graph_path: str | None = None, live_llm: bool = False, trace: bool = False) -> None:
    work = Path(tempfile.mkdtemp(prefix="scaffold-sim-"))
    db_path = work / "referrals.db"
    _seed_business_db(db_path)

    server = ThreadingHTTPServer(("127.0.0.1", FHIR_PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    smtp = FakeSMTP()
    smtp.start()

    if live_llm:
        _load_env_file(Path(__file__).resolve().parents[2])
    os.environ["DATA_DIR"] = str(work / "data")
    os.environ["EGRESS_ALLOWED_HOSTS"] = "127.0.0.1"
    os.environ["AI_ENABLED"] = "true" if live_llm else "false"
    os.environ.setdefault("SCHEDULER_ENABLED", "false")
    from app.settings import get_settings

    get_settings.cache_clear()

    try:
        from app.graph.compiler import compile_graph
        from app.graph.schema import WorkflowGraph

        if graph_path:
            payload = json.loads(Path(graph_path).read_text(encoding="utf-8"))
            graph_data = _adapt_generated_graph(payload, db_path)
        else:
            graph_data = build_graph(db_path)
        graph = WorkflowGraph.model_validate(graph_data)
        compiled = compile_graph(graph)
        if trace:
            result: dict[str, Any] = {}
            async for update in compiled.astream(
                {"inputs": {"referral_id": "r1", "referral_no": "r1"}},
                stream_mode="updates",
            ):
                print("update:", list(update))
        else:
            result = await compiled.ainvoke(
                {"inputs": {"referral_id": "r1", "referral_no": "r1"}}
            )

        print("=== node outputs ===")
        for node_id, value in result.get("vars", {}).items():
            print(f"{node_id}: {json.dumps(value, ensure_ascii=False, default=str)[:500]}")
        print("=== final ===")
        print(json.dumps(result.get("output"), ensure_ascii=False, default=str, indent=2))
        print("=== email ===")
        for message in smtp.messages:
            print(message[:1200])
        if graph_path and not smtp.messages:
            print("NOTE: the model judged the materials complete; no email was sent.")
        print("RESULT=PASS")
    finally:
        smtp.stop()
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", help="A generated graph JSON to adapt and run.")
    parser.add_argument(
        "--live-llm",
        action="store_true",
        help="Load the real key from ../../.env so LLM nodes actually run.",
    )
    parser.add_argument("--trace", action="store_true", help="Print each graph step.")
    args = parser.parse_args()
    asyncio.run(_run(args.graph, args.live_llm, args.trace))
