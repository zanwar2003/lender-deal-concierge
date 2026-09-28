"""A tiny local web server for the chat widget and the reviewer queue.

    python -m concierge serve        # then open http://localhost:8765

Standard library only. Endpoints:
    GET  /                   chat widget (as it would appear on a lender's site)
    GET  /review             reviewer page: pending drafts with approve / reject
    POST /api/chat           {"session_id", "message"} -> {"reply"}
    GET  /api/queue          all queue items
    POST /api/queue/<id>     {"approve": true|false, "reviewer": "..."}

Sessions live in memory. This is a demo server, not a production deployment:
a real one would sit behind authentication (especially /review), rate
limiting, and HTTPS.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agent import Concierge, Session
from .crm import CRMSender

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def make_handler(concierge: Concierge, sender: CRMSender):
    sessions: dict[str, Session] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data).encode(), "application/json")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length) or b"{}")

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, (WEB_DIR / "widget.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/review":
                self._send(200, (WEB_DIR / "review.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/greeting":
                self._json({"session_id": uuid.uuid4().hex, "reply": concierge.greeting()})
            elif self.path == "/api/queue":
                self._json([asdict(i) for i in reversed(concierge.queue.list())])
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            try:
                body = self._body()
            except json.JSONDecodeError:
                return self._json({"error": "invalid JSON"}, 400)
            if self.path == "/api/chat":
                message = str(body.get("message", "")).strip()[:2000]
                if not message:
                    return self._json({"error": "empty message"}, 400)
                sid = str(body.get("session_id") or uuid.uuid4().hex)
                session = sessions.setdefault(sid, Session(sid))
                reply = concierge.reply(session, message)
                return self._json({"session_id": sid, "reply": reply,
                                   "handed_off": session.handed_off})
            if self.path.startswith("/api/queue/"):
                item_id = self.path.rsplit("/", 1)[-1]
                try:
                    item = concierge.queue.decide(item_id, bool(body.get("approve")),
                                                  str(body.get("reviewer") or "reviewer"), sender)
                except KeyError:
                    return self._json({"error": "no such item"}, 404)
                except ValueError as exc:
                    return self._json({"error": str(exc)}, 409)
                return self._json(asdict(item))
            return self._json({"error": "not found"}, 404)

    return Handler


def serve(concierge: Concierge, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = ThreadingHTTPServer((host, port), make_handler(concierge, CRMSender()))
    print(f"Concierge widget: http://{host}:{port}/")
    print(f"Reviewer queue:   http://{host}:{port}/review")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
