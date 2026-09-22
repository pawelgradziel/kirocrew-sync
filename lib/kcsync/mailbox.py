"""Session mailbox: move one KiroCrew session between machines through the
configured backend, using KiroCrew's own export and import routes.

kirocrew-sync.sh owns the transport (it calls the backend's mailbox_* helpers).
This module owns the two ends that touch KiroCrew:

* ``mailbox-export`` asks the local gateway for ``GET
  /api/chat/slots/{slot}/export``, stamps the sender label into the bundle's
  top-level ``origin`` and writes the gzipped file for upload.
* ``mailbox-import`` hands a downloaded file to ``POST /api/chat/slots/import``
  and records it in a local state file, so the same bundle is never installed
  twice. Upstream import is copy-never-move: a second install is a second,
  duplicate session, not a no-op.

Neither end reads or writes KiroCrew's data directories. Everything goes
through the running gateway, which does its own validation, redaction, size
caps and ``Imported / from <sender>`` filing.

Authentication mirrors what KiroCrew's own local tooling does
(``kirocrew token``, ``app_lifecycle_client.toggle_app``) and what this repo's
install-app.sh already does: read the per-gateway secret from the data home,
mint a short-lived dashboard token with ``GET /api/token/local`` and an
``X-Local-Secret`` header, then call the routes with ``?token=``. The
dashboard's unix socket is preferred when it exists (it sits in the 0700 data
home and the gateway checks the peer's credentials); loopback TCP otherwise.
"""

import gzip
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import sys
import urllib.parse
import zlib
from datetime import datetime, timezone
from pathlib import Path

#: Upstream's export suffix (session_export.EXPORT_FILE_SUFFIX). Mailbox files
#: keep it so a file that leaves the mailbox is still recognisable by name, and
#: so KiroCrew's own "Import a session from a file" row accepts it as is.
SUFFIX = ".kcsession.json.gz"

#: The broadcast address: every machine on the backend except the sender.
BROADCAST = "all"

DEFAULT_PORT = 5476

#: The gateway's aiohttp ``client_max_size`` (session_transfer
#: ``_GATEWAY_CLIENT_MAX_SIZE``). A larger body is refused before it is read,
#: so say so here instead of uploading 60 MiB to be told the same thing.
MAX_UPLOAD_BYTES = 60 * 1024 * 1024

#: Bound on how far we will inflate an export to stamp ``origin``. The export
#: comes from our own gateway, which caps the content at 20M chars plus 40M of
#: Layer B, so this only exists to make a corrupt file fail rather than eat
#: memory.
MAX_INFLATE_BYTES = 128 * 1024 * 1024

TOKEN_TTL = "10m"

# Exit codes the shell side maps to messages. 1 stays "anything else".
EXIT_GATEWAY_DOWN = 2
EXIT_AUTH = 3
EXIT_REFUSED = 4


class MailboxError(Exception):
    def __init__(self, message, code=1):
        super().__init__(message)
        self.code = code


def _err(message):
    sys.stderr.write(message + "\n")


# --------------------------------------------------------------------------
# Names

def sanitize(name):
    """Same normalisation kirocrew-sync.sh's get_machine_id applies, so a
    label is always a single path segment and never contains ``--`` (the
    field separator in mailbox file names)."""
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name or "")
    name = re.sub(r"-{2,}", "-", name).strip("-")
    # A leading dot would hide the file from listings, and ".." is a path.
    return name.lstrip(".")


def _stamp():
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "%s-%s" % (now, secrets.token_hex(3))


def mailbox_filename(sender, slug):
    """``<stamp>--<sender>--<slug>.kcsession.json.gz``.

    Stamp first so a listing sorts oldest to newest. The random tail keeps two
    sends in the same second apart. Neither the stamp nor a sanitized sender
    contains ``--``, so the name splits unambiguously even when the slug does.
    """
    slug = sanitize(slug)[:60] or "session"
    return "%s--%s--%s%s" % (_stamp(), sanitize(sender), slug, SUFFIX)


def parse_filename(name):
    """Return ``(stamp, sender, slug)`` or ``None`` for a foreign file."""
    if not name.endswith(SUFFIX) or name.startswith("."):
        return None
    parts = name[: -len(SUFFIX)].split("--", 2)
    if len(parts) != 3 or not all(parts):
        return None
    return tuple(parts)


def _slug_from_disposition(header):
    """Recover upstream's title slug from ``Content-Disposition``.

    Upstream builds it from the already-redacted title, and calls the filename
    an egress surface; reusing its slug rather than the raw title keeps that
    property here. The name is ``<slug>-<stamp>.kcsession.json.gz``.
    """
    if not header:
        return "session"
    m = re.search(r"filename\*=UTF-8''([^;]+)", header)
    if m:
        filename = urllib.parse.unquote(m.group(1))
    else:
        m = re.search(r'filename="?([^";]+)"?', header)
        filename = m.group(1) if m else ""
    if filename.endswith(SUFFIX):
        filename = filename[: -len(SUFFIX)]
    filename = re.sub(r"-\d{8}T\d{6}Z$", "", filename)
    return filename or "session"


# --------------------------------------------------------------------------
# Gateway discovery and transport

def _config_url_port(kirocrew_dir):
    """Port explicitly named by ``dashboard.url`` in KiroCrew's config.json."""
    try:
        cfg = json.loads((kirocrew_dir / "config.json").read_text(encoding="utf-8"))
        url = (cfg.get("dashboard") or {}).get("url") or ""
        if not isinstance(url, str) or not url:
            return None
        if "://" not in url:
            url = "http://" + url
        return urllib.parse.urlparse(url).port
    except Exception:
        return None


def _marker_port(kirocrew_dir):
    """The sole ``run/gateway-<port>.bin`` marker's port, or ``None``.

    Upstream's own client resolution (port_resolution._marker_port) also
    verifies that the marker's PID is a KiroCrew process owned by this user;
    that check needs /proc parsing we do not replicate. We refuse ambiguity the
    same way and rely on the health probe before the secret is ever sent.
    """
    ports = []
    for marker in (kirocrew_dir / "run").glob("gateway-*.bin"):
        m = re.match(r"gateway-(\d+)\.bin$", marker.name)
        if m:
            ports.append(int(m.group(1)))
    return ports[0] if len(ports) == 1 else None


def resolve_port(kirocrew_dir):
    """Same order as upstream's resolve_client_port, minus the steps that need
    KiroCrew's own code: KIROCREW_PORT, then KIROCREW_BOUND_PORT, then a port
    written in dashboard.url, then the sole run marker, then 5476."""
    for var in ("KIROCREW_PORT", "KIROCREW_BOUND_PORT"):
        raw = os.environ.get(var, "")
        if raw.strip().isdigit():
            return int(raw), var
    port = _config_url_port(kirocrew_dir)
    if port:
        return port, "dashboard.url in config.json"
    port = _marker_port(kirocrew_dir)
    if port:
        return port, "run/gateway-%d.bin" % port
    return DEFAULT_PORT, "default"


def read_secret(kirocrew_dir, port):
    """Per-listener secret first, then the shared one -- upstream's
    config.loader.read_local_secret order."""
    for path in (kirocrew_dir / "run" / ("gateway-%d.secret" % port),
                 kirocrew_dir / ".local_secret"):
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, host, port, timeout):
        super().__init__(host, port, timeout=timeout)
        self._socket_path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class Gateway:
    """The local KiroCrew gateway, authenticated as the dashboard owner."""

    def __init__(self, kirocrew_dir, timeout=120):
        self.kirocrew_dir = Path(kirocrew_dir)
        self.timeout = timeout
        self.token = ""
        explicit = os.environ.get("KCSYNC_GATEWAY_URL", "").strip()
        if explicit:
            parsed = urllib.parse.urlparse(explicit)
            # The local secret is only ever sent to this machine.
            if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise MailboxError(
                    "KCSYNC_GATEWAY_URL must point at this machine (127.0.0.1, "
                    "localhost or ::1); the gateway secret is never sent elsewhere.")
            self.host = parsed.hostname
            self.port = parsed.port or 80
            self.port_source = "KCSYNC_GATEWAY_URL"
            self.socket_path = None
        else:
            self.host = "127.0.0.1"
            self.port, self.port_source = resolve_port(self.kirocrew_dir)
            sock = self.kirocrew_dir / ("dashboard-%d.sock" % self.port)
            self.socket_path = str(sock) if sock.exists() else None

    @property
    def where(self):
        tcp = "%s:%d" % (self.host, self.port)
        if self.socket_path:
            return "%s (socket %s)" % (tcp, self.socket_path)
        return tcp

    def _connection(self):
        if self.socket_path:
            return _UnixHTTPConnection(self.socket_path, self.host, self.port,
                                       self.timeout)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def request(self, method, path, body=None, headers=None, query=None):
        query = dict(query or {})
        if self.token:
            query["token"] = self.token
        if query:
            path = path + "?" + urllib.parse.urlencode(query)
        attempts = 2 if self.socket_path else 1
        for attempt in range(attempts):
            conn = self._connection()
            try:
                conn.request(method, path, body=body, headers=headers or {})
                resp = conn.getresponse()
                return resp.status, dict(resp.getheaders()), resp.read()
            except (ConnectionRefusedError, FileNotFoundError) as exc:
                if self.socket_path and attempt == 0:
                    # A stale socket file from a gateway that has exited. The
                    # TCP listener is the next best evidence of a live one.
                    self.socket_path = None
                    continue
                raise MailboxError(
                    "KiroCrew is not running (nothing answered at %s: %s).\n"
                    "Start KiroCrew (kirocrew gateway), or set KIROCREW_PORT if it "
                    "listens on a non-default port. Port came from: %s."
                    % (self.where, exc.__class__.__name__, self.port_source),
                    EXIT_GATEWAY_DOWN)
            except (OSError, http.client.HTTPException) as exc:
                raise MailboxError(
                    "Could not talk to the KiroCrew gateway at %s: %s"
                    % (self.where, exc), EXIT_GATEWAY_DOWN)
            finally:
                conn.close()
        raise MailboxError("unreachable", EXIT_GATEWAY_DOWN)  # pragma: no cover

    def authenticate(self):
        status, _, _ = self.request("GET", "/api/health")
        if status >= 500 or status == 404:
            raise MailboxError(
                "Something answered at %s, but not a healthy KiroCrew gateway "
                "(GET /api/health -> HTTP %d)." % (self.where, status),
                EXIT_GATEWAY_DOWN)

        secret = read_secret(self.kirocrew_dir, self.port)
        if not secret:
            raise MailboxError(
                "No gateway credential to authenticate with. Looked for "
                "%s and %s.\nThe gateway writes these when it starts; is KiroCrew "
                "running as this user, with KIROCREW_DIR=%s?"
                % (self.kirocrew_dir / "run" / ("gateway-%d.secret" % self.port),
                   self.kirocrew_dir / ".local_secret", self.kirocrew_dir),
                EXIT_AUTH)

        status, _, body = self.request(
            "GET", "/api/token/local", headers={"X-Local-Secret": secret},
            query={"ttl": TOKEN_TTL})
        if status != 200:
            raise MailboxError(
                "The gateway refused a local token (HTTP %d): %s\n"
                "The secret under %s may belong to a different gateway than the "
                "one on port %d, or the gateway could not verify this process as "
                "its local owner." % (status, _error_text(body), self.kirocrew_dir,
                                      self.port),
                EXIT_AUTH)
        try:
            token = json.loads(body.decode("utf-8")).get("token", "")
        except Exception:
            token = ""
        if not token:
            raise MailboxError("The gateway returned no token.", EXIT_AUTH)
        self.token = token


def _error_text(body):
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        if isinstance(data, dict):
            text = str(data.get("error") or "")
            code = str(data.get("code") or "")
            if text and code:
                return "%s [%s]" % (text, code)
            return text or code or "no detail"
    except Exception:
        pass
    text = body.decode("utf-8", "replace").strip()
    return text[:300] or "no detail"


# --------------------------------------------------------------------------
# State: what this machine installed or discarded. Local, never synced: it
# lives under $KIROCREW_DIR/.sync, which no ALLOW glob reaches.

def load_state(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("installed", {})
            data.setdefault("names", {})
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "installed": {}, "names": {}}


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Commands

def cmd_export(args):
    slot = args.slot
    # Accept a session key as well as a slot key: dashboard tabs are keyed
    # "dashboard:<slot>", and that is the form most places print.
    if slot.startswith("dashboard:"):
        slot = slot[len("dashboard:"):]
    if not slot or "/" in slot:
        raise MailboxError("Not a slot key: %r" % args.slot)

    gw = Gateway(args.kirocrew_dir)
    gw.authenticate()

    query = {"include_layer_b": "true"} if args.include_layer_b else None
    status, headers, body = gw.request(
        "GET", "/api/chat/slots/%s/export" % urllib.parse.quote(slot, safe=""),
        query=query)
    if status != 200:
        hint = ""
        if status == 404:
            # The route looks the key up among the gateway's loaded slots, so a
            # session that exists on disk but is not loaded answers 404 too.
            hint = ("\nThe slot must be a session KiroCrew currently has loaded "
                    "(a tab in the dashboard). Use its slot key, or its session "
                    "key 'dashboard:<slot>'.")
        raise MailboxError(
            "KiroCrew refused to export %r (HTTP %d): %s%s"
            % (slot, status, _error_text(body), hint), EXIT_REFUSED)
    if body[:2] != b"\x1f\x8b":
        raise MailboxError("The export was not a gzip file; refusing to send it.")

    try:
        dobj = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = dobj.decompress(body, MAX_INFLATE_BYTES + 1)
        if len(raw) > MAX_INFLATE_BYTES or dobj.unconsumed_tail:
            raise MailboxError("The export inflates past %d MiB; refusing to send it."
                               % (MAX_INFLATE_BYTES // (1024 * 1024)))
        bundle = json.loads(raw.decode("utf-8"))
    except MailboxError:
        raise
    except Exception as exc:
        raise MailboxError("Could not read the export: %s" % exc)
    if not isinstance(bundle, dict):
        raise MailboxError("The export is not a session bundle.")

    # Upstream's file export blanks `origin` on purpose: a downloaded file can
    # go anywhere, and the host name can carry a login. A mailbox file goes to
    # a backend whose readers already see this label in every bundle name, and
    # the receiver needs it for "Imported / from <sender>" filing, so it is put
    # back -- as the label, never the hostname. A top-level string: nothing
    # inside Layer B (which is signed) is touched.
    if not bundle.get("origin"):
        bundle["origin"] = args.origin
    out_bytes = gzip.compress(
        json.dumps(bundle, separators=(",", ":")).encode("utf-8"), mtime=0)
    if len(out_bytes) > MAX_UPLOAD_BYTES:
        raise MailboxError("The bundle is %d MiB; the receiving gateway accepts at "
                           "most 60 MiB." % (len(out_bytes) // (1024 * 1024)))

    Path(args.out).write_bytes(out_bytes)
    slug = _slug_from_disposition(headers.get("Content-Disposition", ""))
    name = mailbox_filename(args.origin, slug)

    if bundle.get("layer_b"):
        layer = "with Layer B (resumes via session/load; unredacted model context)"
    elif bundle.get("layer_b_skipped"):
        layer = "transcript only (Layer B withheld)"
    else:
        layer = "transcript only"
    messages = bundle.get("messages") or []
    _err("  exported %d message(s), %s, %d bytes"
         % (len(messages), layer, len(out_bytes)))
    if args.include_layer_b and not bundle.get("layer_b"):
        _err("  --include-layer-b was ignored by KiroCrew: it also needs "
             "dashboard.export_include_layer_b=true in KiroCrew's config.json")
    print(name)
    return 0


def cmd_check(args):
    """Exit 0 if NAME is new (neither installed nor discarded here)."""
    state = load_state(args.state)
    return 1 if args.name in state["names"] else 0


def cmd_import(args):
    state = load_state(args.state)
    data = Path(args.file).read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    previous = state["installed"].get(digest)
    if previous and not args.force:
        # Same bytes under another name: a broadcast plus a direct send, or a
        # re-upload. Import would make a second, duplicate session.
        state["names"][args.name] = {"status": "installed", "sha256": digest,
                                     "at": _now(), "duplicate_of": previous.get("name")}
        save_state(args.state, state)
        _err("  already installed as %s (from %s); skipped"
             % (previous.get("slot") or "?", previous.get("name")))
        print("duplicate")
        return 0

    if len(data) > MAX_UPLOAD_BYTES:
        raise MailboxError("%s is %d MiB; the gateway accepts at most 60 MiB."
                           % (args.name, len(data) // (1024 * 1024)), EXIT_REFUSED)
    if data[:2] != b"\x1f\x8b":
        raise MailboxError("%s is not a gzip file." % args.name, EXIT_REFUSED)

    gw = Gateway(args.kirocrew_dir)
    gw.authenticate()
    status, _, body = gw.request(
        "POST", "/api/chat/slots/import", body=data,
        headers={"Content-Type": "application/gzip",
                 "Content-Length": str(len(data))})
    if status != 200:
        raise MailboxError("KiroCrew refused %s (HTTP %d): %s"
                           % (args.name, status, _error_text(body)), EXIT_REFUSED)
    try:
        result = json.loads(body.decode("utf-8"))
    except Exception:
        result = {}

    record = {
        "name": args.name,
        "slot": result.get("key", ""),
        "title": result.get("title", ""),
        "messages": result.get("messages"),
        "resume_mode": result.get("resume_mode", ""),
        "installed_at": _now(),
    }
    state["installed"][digest] = record
    state["names"][args.name] = {"status": "installed", "sha256": digest,
                                 "at": record["installed_at"]}
    save_state(args.state, state)
    _err("  installed as %s: %s (%s message(s)%s)"
         % (record["slot"] or "a new session", record["title"] or "untitled",
            record["messages"] if record["messages"] is not None else "?",
            ", " + record["resume_mode"] if record["resume_mode"] else ""))
    print("installed")
    return 0


def cmd_discard(args):
    state = load_state(args.state)
    state["names"][args.name] = {"status": "discarded", "at": _now()}
    save_state(args.state, state)
    return 0


def cmd_list(args):
    """Format a listing read from stdin: ``<address> <name> <size>`` lines."""
    state = load_state(args.state)
    rows = []
    for line in sys.stdin:
        parts = line.split()
        if len(parts) < 2:
            continue
        address, name = parts[0], parts[1]
        size = parts[2] if len(parts) > 2 else "?"
        parsed = parse_filename(name)
        if parsed is None:
            continue
        stamp, sender, slug = parsed
        if address == BROADCAST and sender in args.me:
            continue  # our own broadcast
        status = (state["names"].get(name) or {}).get("status", "new")
        if status != "new" and not args.all:
            continue
        rows.append((stamp, name, sender, address, size, status))

    if args.names_only:
        for row in sorted(rows):
            if row[5] == "new":
                print("%s %s" % (row[3], row[1]))
        return 0

    if not rows:
        print("  (no session bundles waiting)")
        return 0
    for stamp, name, sender, address, size, status in sorted(rows):
        when = stamp.split("-")[0]
        try:
            when = datetime.strptime(when, "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M UTC")
        except ValueError:
            pass
        to = "everyone" if address == BROADCAST else "you"
        human = size
        if size.isdigit():
            n = int(size)
            human = "%d KB" % max(1, n // 1024) if n < 1024 * 1024 else "%.1f MB" % (n / 1048576)
        tag = "" if status == "new" else "  [%s]" % status
        print("  %s%s" % (name, tag))
        print("      from %s to %s, %s, %s" % (sender, to, when, human))
    return 0


def add_parsers(sub, default_dir):
    p = sub.add_parser("mailbox-export",
                       help="export one session from the running gateway")
    p.add_argument("--kirocrew-dir", default=default_dir)
    p.add_argument("--slot", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--origin", required=True, help="sender label to stamp")
    p.add_argument("--include-layer-b", action="store_true")
    p.set_defaults(func=_guard(cmd_export))

    p = sub.add_parser("mailbox-import",
                       help="install one downloaded bundle through the gateway")
    p.add_argument("--kirocrew-dir", default=default_dir)
    p.add_argument("--file", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=_guard(cmd_import))

    p = sub.add_parser("mailbox-check", help="exit 0 if a bundle name is new here")
    p.add_argument("--state", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("mailbox-discard", help="record a bundle as discarded")
    p.add_argument("--state", required=True)
    p.add_argument("--name", required=True)
    p.set_defaults(func=cmd_discard)

    p = sub.add_parser("mailbox-list", help="format an inbox listing from stdin")
    p.add_argument("--state", required=True)
    p.add_argument("--me", action="append", default=[],
                   help="this machine's own labels (repeatable)")
    p.add_argument("--all", action="store_true",
                   help="include installed and discarded bundles")
    p.add_argument("--names-only", action="store_true",
                   help="print '<address> <name>' for new bundles only")
    p.set_defaults(func=cmd_list)


def _guard(func):
    def run(args):
        try:
            return func(args)
        except MailboxError as exc:
            _err(str(exc))
            return exc.code
    return run
