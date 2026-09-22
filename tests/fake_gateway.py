#!/usr/bin/env python3
"""A stand-in for the KiroCrew gateway, for tests/test_session_mailbox.sh.

Implements exactly the surface send-session and inbox use, shaped after
upstream (kiro_crew/dashboard/session_export.py, session_transfer.py and
handlers/core.py:api_token_local):

  GET  /api/health                      200, no auth
  GET  /api/token/local                 X-Local-Secret -> {"token", "expires_in"}
  GET  /api/chat/slots/{slot}/export    ?token= -> gzip bundle, origin ""
  POST /api/chat/slots/import           ?token= -> {"ok", "key", "title", ...}

Every import is appended to <state>/imports.jsonl and every export request
to <state>/exports.jsonl, so the test can assert what the gateway was asked.
Nothing here writes anywhere but --state.

Usage: fake_gateway.py --secret S --state DIR --slots FILE
                       (--port-file F | --unix PATH)
"""

import argparse
import gzip
import hashlib
import itertools
import json
import os
import socketserver
import sys
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-kirocrew/0"

    def log_message(self, *args):  # quiet
        pass

    def address_string(self):
        return "local"

    # -- helpers ----------------------------------------------------------
    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self, query):
        return query.get("token", [""])[0] in self.server.tokens

    def _log(self, name, record):
        with open(os.path.join(self.server.state, name), "a") as f:
            f.write(json.dumps(record) + "\n")

    # -- routes -----------------------------------------------------------
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)

        if url.path == "/api/health":
            return self._json(200, {"ok": True})

        if url.path == "/api/token/local":
            if os.environ.get("FAKE_REFUSE_OWNER") == "1":
                return self._json(403, {
                    "error": "The gateway could not verify this process as the local owner.",
                    "code": "member_owner_token_refused"})
            if self.headers.get("X-Local-Secret", "") != self.server.secret:
                return self._json(403, {"error": "invalid secret"})
            token = "tok-%d" % next(self.server.counter)
            self.server.tokens.add(token)
            return self._json(200, {"token": token, "expires_in": 600})

        parts = url.path.split("/")
        # /api/chat/slots/{slot}/export
        if len(parts) == 6 and parts[1:4] == ["api", "chat", "slots"] and parts[5] == "export":
            if not self._authed(query):
                return self._json(401, {"error": "Token required"})
            slot = urllib.parse.unquote(parts[4])
            layer_b_asked = query.get("include_layer_b", [""])[0] == "true"
            self._log("exports.jsonl", {"slot": slot, "include_layer_b": layer_b_asked})
            slots = json.load(open(self.server.slots_file))
            if slot not in slots:
                return self._json(404, {"error": "session not found",
                                        "code": "export_slot_not_found"})
            src = slots[slot]
            bundle = {
                "bundle_version": 2,
                "origin": "",  # the file export always blanks it
                "title": src["title"],
                "agent": "",
                "messages": src["messages"],
            }
            if src.get("layer_b") and layer_b_asked and os.environ.get("FAKE_LAYER_B_PERMITTED") == "1":
                bundle["layer_b"] = src["layer_b"]
            elif src.get("layer_b"):
                bundle["layer_b_skipped"] = True
            body = gzip.compress(json.dumps(bundle, separators=(",", ":")).encode(), mtime=0)
            slug = src["title"].lower().replace(" ", "-")
            filename = "%s-20260922T101500Z.kcsession.json.gz" % slug
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''" + urllib.parse.quote(filename, safe=""))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if url.path != "/api/chat/slots/import":
            return self._json(404, {"error": "not found"})
        if not self._authed(query):
            return self._json(401, {"error": "Token required"})
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        digest = hashlib.sha256(raw).hexdigest()
        if raw[:2] == b"\x1f\x8b":
            try:
                raw = zlib.decompress(raw, 16 + zlib.MAX_WBITS)
            except Exception:
                return self._json(400, {"error": "could not decompress the bundle",
                                        "code": "transfer_invalid_gzip"})
        try:
            bundle = json.loads(raw)
        except Exception:
            return self._json(400, {"error": "invalid JSON body",
                                    "code": "transfer_invalid_json"})
        if bundle.get("bundle_version") not in (1, 2):
            return self._json(400, {"error": "unsupported bundle_version",
                                    "code": "transfer_version_unsupported"})
        if not bundle.get("messages"):
            return self._json(400, {"error": "bundle carries no messages",
                                    "code": "transfer_bundle_empty"})
        key = "slot-imported-%d" % next(self.server.counter)
        origin = bundle.get("origin", "")
        title = bundle.get("title") or "Untitled"
        self._log("imports.jsonl", {
            "key": key, "origin": origin, "title": title,
            "messages": len(bundle["messages"]),
            "layer_b": bool(bundle.get("layer_b")),
            "body_sha256": digest,
        })
        self._json(200, {
            "ok": True, "key": key,
            "title": title + (" (from %s)" % origin if origin else ""),
            "messages": len(bundle["messages"]),
            "resume_mode": "session_load" if bundle.get("layer_b") else "history_prefix",
        })


class TCPServer(HTTPServer):
    pass


class UnixServer(socketserver.UnixStreamServer):
    def get_request(self):
        request, _ = super().get_request()
        return request, ("local", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secret", required=True)
    ap.add_argument("--state", required=True)
    ap.add_argument("--slots", required=True)
    ap.add_argument("--port-file")
    ap.add_argument("--unix")
    args = ap.parse_args()

    if args.unix:
        server = UnixServer(args.unix, Handler)
    else:
        server = TCPServer(("127.0.0.1", 0), Handler)
    server.secret = args.secret
    server.state = args.state
    server.slots_file = args.slots
    server.tokens = set()
    server.counter = itertools.count(1)
    if args.port_file:
        tmp = args.port_file + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(server.server_address[1]))
        os.replace(tmp, args.port_file)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
