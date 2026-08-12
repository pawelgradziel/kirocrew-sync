"""
Notification service - pushes actionable KiroCrew notifications for sync events.

Real transport (verified against the KiroCrew host source at
``/home/pawel/code/kirocrew``, read-only, Python/``src/kiro_crew/``):

This app declares ``backend.entryPoint`` in ``app.json`` (not
``backend.routes``), so ``register_builtin_apps``/``manager.py`` (around
``dest / ".app_secret"``, see ``src/kiro_crew/apps/manager.py:1253``) writes
this app a per-app secret at ``~/.kiro/crew/apps/kirocrew-sync/.app_secret``.
That is exactly the credential an *out-of-process* app backend uses to talk
to the gateway over HTTP - there is no in-process notification bus to import
here (that path, ``kiro_crew.notifications.bus`` /
``apps/builtins/*/backend/notify_out.py``, only exists for builtin apps
running inside the gateway's own Python process; see
``src/kiro_crew/apps/builtins/ops_mission_control/backend/notify_out.py``
for the in-process equivalent of this module).

The out-of-process call path (confirmed by reading the gateway handlers):

1. ``POST /api/apps/<app_name>/token`` with header ``X-App-Secret: <secret>``
   exchanges the on-disk secret for a short-lived app-scoped token.
   Handler: ``src/kiro_crew/dashboard/handlers/core.py:1893`` (``api_app_token``).
2. ``POST /api/notifications/push?token=<token>`` with a JSON body of
   ``{channel, title, body, group_key, url, actions, ttl}`` delivers the
   notification through the bus on the app's behalf. Handler:
   ``src/kiro_crew/dashboard/handlers/notifications_push.py:58``
   (``api_push_notification``). The channel must be one this app declared in
   ``app.json`` -> ``notifications.channels`` (verified against
   ``_resolve_app_channels`` in the same file), and delivery is rate-limited
   and silently denied if the app is disabled - both handled here by simply
   returning ``False``.

   The token is passed as ``?token=`` rather than a cookie: the shipped
   ``kirocrew_client`` Python package (``packages/kirocrew-client-py``) sends
   a plain ``Cookie: mc_token=...``, but the gateway's auth middleware keys
   dashboard cookies as ``mc_token_<port>`` (see
   ``src/kiro_crew/dashboard/token_auth.py:1088`` and the extraction at
   line ``1305``/``1539``, both of which try ``request.query.get("token")``
   first). The query parameter is the one path in that code that does not
   depend on a port-specific cookie name, so it is the reliable choice for a
   plain HTTP client outside a browser.

This module intentionally does not depend on ``kirocrew_client`` (that
package is not in ``app/requirements.txt``, and its own ``send_notification``
helper posts to ``/api/send-message`` - agent chat messaging - not
``/api/notifications/push``, so it would not even be the right call).
Everything here is stdlib ``urllib`` only, per the no-external-deps style of
``history.py``.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .database import Database
from .models import Conflict

logger = logging.getLogger(__name__)


#: Must match app.json's "name" - the gateway namespaces this app's channels
#: as "<app_name>.<channel_id>" and resolves the on-disk secret from this name.
APP_NAME = "kirocrew-sync"

#: Channel ids, mirroring app.json -> notifications.channels exactly (do not
#: invent new ones here - the gateway refuses a push to an undeclared channel).
CHANNEL_SYNC_COMPLETED = "sync-completed"
CHANNEL_CONFLICT_DETECTED = "conflict-detected"
CHANNEL_QUARANTINE = "quarantine"
CHANNEL_SYNC_FAILED = "sync-failed"

#: Default priorities as declared in app.json, kept here for documentation
#: only - NOT sent on the wire. The host's channel-priority enum
#: (``_CHANNEL_PRIORITIES`` in ``src/kiro_crew/apps/manifest.py:788``) is only
#: ("critical", "default", "passive"), and ``NotificationsConfig.validate()``
#: rejects the whole manifest for anything outside it. app.json originally
#: declared "medium" for conflict-detected, which failed that check and would
#: have made the app unloadable; it is now "default". These values are still
#: not sent as an explicit per-push override - omitting the priority field
#: lets the gateway fall back to the channel's *registered* default, which is
#: the same value and keeps app.json the single source of truth.
DECLARED_PRIORITIES = {
    CHANNEL_SYNC_COMPLETED: "passive",
    CHANNEL_CONFLICT_DETECTED: "default",
    CHANNEL_QUARANTINE: "critical",
    CHANNEL_SYNC_FAILED: "critical",
}

#: Dashboard deep-link base, per docs/kirocrew-app-plan.md Phase 4.
_DASHBOARD_BASE = "/apps/kirocrew-sync"

#: Where the gateway writes this app's per-app secret on install
#: (``manager.py``'s ``dest / ".app_secret"``), matching the layout the rest
#: of this backend already assumes (see database.py, backends.py).
_APP_DIR = Path.home() / ".kiro" / "crew" / "apps" / APP_NAME
_APP_SECRET_PATH = _APP_DIR / ".app_secret"

#: Default dedup window (seconds). Matches the cron cadence in app.json
#: ("every": 300) - a quarantine or conflict condition that is still true on
#: the next cron tick must not re-fire.
DEFAULT_DEDUP_WINDOW_SECONDS = 300.0

_REQUEST_TIMEOUT_SECONDS = 5.0


class _TransportError(Exception):
    """Internal: any failure talking to the gateway. Never escapes this module."""


def _base_url() -> str:
    """Gateway base URL. Same env var / default port as ``kirocrew_client``."""
    port = os.environ.get("KIROCREW_PORT", "5476")
    return f"http://localhost:{port}"


def _http_post_json(
    url: str, headers: Dict[str, str], payload: Optional[Dict[str, Any]], timeout: float
) -> Tuple[int, Any]:
    """Synchronous POST returning (status_code, parsed_json_or_None).

    Runs on a worker thread (see ``NotificationService._post``) - this
    function itself is blocking I/O and must never be awaited directly.
    Raises ``_TransportError`` on anything that isn't a well-formed HTTP
    response (DNS/connection failure, timeout, malformed JSON).
    """
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        # HTTPError IS the response for non-2xx statuses - read it for the
        # error detail the gateway put in the body, but don't fail parsing it.
        status = exc.code
        try:
            body = exc.read()
        except Exception:
            body = b""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _TransportError(f"request to {url} failed: {exc}") from exc

    parsed: Any = None
    if body:
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
    return status, parsed


class NotificationService:
    """Sends actionable KiroCrew notifications for sync events.

    Every ``notify_*`` method is a fire-and-forget best effort: it never
    raises, and returns ``True`` only when a notification was actually
    dispatched to the gateway. Suppression (a clean sync, a duplicate within
    the dedup window, notifications disabled, or the transport being
    unavailable) returns ``False`` without treating that as an error.

    Transport resolution (locating the app secret) happens lazily on first
    use and is cached for the lifetime of this instance - see
    ``_ensure_transport``. Construct once per process (use
    ``get_notification_service()``) rather than per call.

    Dedup state (see ``_is_duplicate``/``_record_seen``) lives in two
    places at once:

    - An in-memory ``(channel, dedup_key) -> monotonic timestamp`` dict,
      always maintained regardless of ``db_path``. This is the *only*
      dedup available when ``db_path`` is None (e.g. in tests), and is
      always kept as a fallback even when a database is configured.
    - Optionally, the ``notification_dedup`` table in the sqlite database at
      ``db_path`` (see database.py). This is what makes dedup survive
      across separate ``NotificationService`` instances backed by the same
      file - the actual gap this exists to close: the cron CLI
      (backend/cli.py) constructs a fresh instance on every tick and exits
      immediately, so an in-memory-only dict never remembers anything
      between ticks and a persistently failing sync re-notified forever.

    Any failure touching that database - unwritable directory, corrupt
    file, a locked write - is caught and logged, never raised, and falls
    back to the in-memory window for that call. Losing cross-process dedup
    is an acceptable degradation; breaking the sync path that calls these
    methods is not.
    """

    def __init__(
        self,
        enabled: bool = True,
        dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS,
        db_path: Optional[Path] = None,
    ) -> None:
        """
        Args:
            enabled: Master on/off switch. When False, every notify_* call
                is a no-op (still returns False, never raises).
            dedup_window_seconds: How long a (channel, dedup-key) pair
                suppresses a repeat notification. Injectable so tests don't
                have to sleep for 5 minutes; defaults to the app's cron
                cadence (see DEFAULT_DEDUP_WINDOW_SECONDS).
            db_path: Optional sqlite file backing cross-process dedup state
                (see the class docstring). None (the default) means
                dedup is in-memory only for this instance's lifetime -
                the same behavior this class had before database-backed
                dedup existed, which is exactly what most tests want.
                ``get_notification_service()`` passes the app's real
                database path in production.
        """
        self.enabled = enabled
        self._dedup_window = dedup_window_seconds
        self._dedup_seen: Dict[Tuple[str, str], float] = {}

        # Transport resolution cache - see _ensure_transport(). Resolved at
        # most once per instance, never at import time.
        self._transport_checked = False
        self._transport_available = False
        self._app_secret: Optional[str] = None
        self._token: Optional[str] = None

        # Dedup database resolution cache - see _dedup_db_ready(). Like the
        # transport cache above, resolved at most once per instance.
        self._db_path = db_path
        self._db: Optional[Database] = None
        self._dedup_db_checked = False
        self._dedup_db_ok = False

    # -- Public interface ---------------------------------------------------

    async def notify_sync_completed(
        self, run_id: int, rows_merged: int, conflicts: int, quarantine: int
    ) -> bool:
        """Notify that a sync run finished, but only if it needs attention.

        A clean run (no conflicts, no quarantine) sends nothing - a
        successful background sync is not news. Deep-links to whichever tab
        is more urgent: conflicts if any, otherwise quarantine.
        """
        if conflicts <= 0 and quarantine <= 0:
            return False

        tab = "conflicts" if conflicts > 0 else "quarantine"
        parts = []
        if conflicts > 0:
            parts.append(f"{conflicts} conflict{'s' if conflicts != 1 else ''}")
        if quarantine > 0:
            parts.append(f"{quarantine} machine{'s' if quarantine != 1 else ''} quarantined")
        body = f"{rows_merged} rows merged, " + ", ".join(parts)

        return await self._push(
            CHANNEL_SYNC_COMPLETED,
            "Sync completed with issues",
            body,
            dedup_key=f"run:{run_id}",
            url=f"{_DASHBOARD_BASE}?tab={tab}",
            actions=[{"id": "view", "label": "View details", "url": f"{_DASHBOARD_BASE}?tab={tab}"}],
        )

    async def notify_conflict(self, conflict: Conflict) -> bool:
        """Notify that a new conflict needs resolution."""
        return await self._push(
            CHANNEL_CONFLICT_DETECTED,
            f"Conflict on {conflict.machine}",
            f"{conflict.table_name}.{conflict.row_id}",
            dedup_key=f"conflict:{conflict.id}",
            url=f"{_DASHBOARD_BASE}?tab=conflicts&id={conflict.id}",
            actions=[
                {
                    "id": "resolve",
                    "label": "Resolve",
                    "url": f"{_DASHBOARD_BASE}?tab=conflicts&id={conflict.id}",
                }
            ],
        )

    async def notify_quarantine(self, machine: str, reason: str) -> bool:
        """Notify that a machine was quarantined.

        Deduped per machine (not per reason) - the noise this exists to
        avoid is the same machine re-announcing itself every cron cycle
        while it stays quarantined for the same underlying cause.
        """
        return await self._push(
            CHANNEL_QUARANTINE,
            f"Machine quarantined: {machine}",
            reason,
            dedup_key=f"quarantine:{machine}",
            url=f"{_DASHBOARD_BASE}?tab=quarantine",
            actions=[{"id": "view", "label": "View details", "url": f"{_DASHBOARD_BASE}?tab=quarantine"}],
        )

    async def notify_failure(self, error: str, run_id: int | None = None) -> bool:
        """Notify that a sync run failed.

        Deduped on the error text itself (not run_id, which is unique per
        run and would defeat dedup) - a repeatedly failing daemon with an
        unchanged cause should not re-notify every cycle.
        """
        digest = hashlib.sha256(error.strip().encode("utf-8", errors="replace")).hexdigest()[:16]
        return await self._push(
            CHANNEL_SYNC_FAILED,
            "Sync failed",
            error,
            dedup_key=f"failure:{digest}",
            url=f"{_DASHBOARD_BASE}?tab=history",
            actions=[{"id": "view", "label": "View logs", "url": f"{_DASHBOARD_BASE}?tab=history"}],
        )

    # -- Transport ------------------------------------------------------

    def _ensure_transport(self) -> bool:
        """Resolve (once) whether this process can reach the gateway at all.

        Reads the on-disk app secret exactly once per instance and caches
        the result, per the "detect once, don't retry-import on every call"
        requirement. A missing/unreadable secret means this process was not
        launched by the gateway as an installed app (e.g. a bare dev/test
        run) - that is a permanent, expected condition, not a transient
        error, so it is logged at debug level and every subsequent call
        short-circuits to False without touching the filesystem again.
        """
        if self._transport_checked:
            return self._transport_available

        self._transport_checked = True
        try:
            secret = _APP_SECRET_PATH.read_text(encoding="utf-8").strip()
            if not secret:
                raise ValueError("app secret file is empty")
            self._app_secret = secret
            self._transport_available = True
        except Exception as exc:  # noqa: BLE001 - resolution must never raise
            logger.debug(
                "notification transport unavailable: no usable app secret at %s (%s)",
                _APP_SECRET_PATH,
                exc,
            )
            self._transport_available = False
        return self._transport_available

    async def _ensure_token(self, *, force_refresh: bool = False) -> Optional[str]:
        """Return a cached app-scoped token, exchanging the secret if needed."""
        if self._token is not None and not force_refresh:
            return self._token
        if not self._app_secret:
            return None

        url = f"{_base_url()}/api/apps/{APP_NAME}/token"
        headers = {"X-App-Secret": self._app_secret, "Content-Type": "application/json"}
        try:
            status, parsed = await asyncio.to_thread(
                _http_post_json, url, headers, None, _REQUEST_TIMEOUT_SECONDS
            )
        except _TransportError as exc:
            logger.debug("notification token exchange failed: %s", exc)
            return None

        if status != 200 or not isinstance(parsed, dict):
            logger.debug("notification token exchange returned status %s", status)
            return None

        token = parsed.get("token")
        if not token or not isinstance(token, str):
            return None
        self._token = token
        return token

    async def _post(self, path: str, payload: Dict[str, Any]) -> bool:
        """POST an already-built payload, refreshing the token once on 401/403."""
        token = await self._ensure_token()
        if not token:
            return False

        url = f"{_base_url()}{path}?token={token}"
        headers = {"Content-Type": "application/json"}
        try:
            status, parsed = await asyncio.to_thread(
                _http_post_json, url, headers, payload, _REQUEST_TIMEOUT_SECONDS
            )
        except _TransportError as exc:
            logger.debug("notification push failed: %s", exc)
            return False

        if status in (401, 403):
            token = await self._ensure_token(force_refresh=True)
            if not token:
                return False
            url = f"{_base_url()}{path}?token={token}"
            try:
                status, parsed = await asyncio.to_thread(
                    _http_post_json, url, headers, payload, _REQUEST_TIMEOUT_SECONDS
                )
            except _TransportError as exc:
                logger.debug("notification push retry failed: %s", exc)
                return False

        if status == 200 and isinstance(parsed, dict) and parsed.get("ok"):
            return True

        logger.debug(
            "notification push to %s did not succeed: status=%s body=%s", path, status, parsed
        )
        return False

    # -- Dedup ------------------------------------------------------------

    def _dedup_db_ready(self) -> bool:
        """Resolve (once) whether the dedup database can be reached at all.

        Mirrors ``_ensure_transport``'s "detect once, cache the verdict"
        shape: ``Database(...).initialize()`` both validates that the
        directory/file is usable and ensures the ``notification_dedup``
        table exists (idempotent - ``CREATE TABLE IF NOT EXISTS``), so a
        caller never has to have initialized the schema first.

        Only this *structural* check is cached. A path that fails here
        (unwritable directory, corrupt file) is unlikely to start working
        later in the same process, so the negative result short-circuits
        every subsequent call without touching the filesystem again. A
        transient failure on an already-validated path (a locked write, a
        one-off I/O error) is deliberately NOT cached here - see
        ``_is_duplicate``/``_record_seen``, which catch those per call and
        fall back without permanently disabling the database.
        """
        if self._db_path is None:
            return False
        if self._dedup_db_checked:
            return self._dedup_db_ok

        self._dedup_db_checked = True
        try:
            db = Database(self._db_path)
            db.initialize()
            self._db = db
            self._dedup_db_ok = True
        except Exception as exc:  # noqa: BLE001 - resolution must never raise
            logger.debug(
                "notification dedup database unavailable at %s: %s", self._db_path, exc
            )
            self._dedup_db_ok = False
        return self._dedup_db_ok

    def _is_duplicate(self, channel_id: str, dedup_key: str) -> bool:
        if self._dedup_db_ready():
            try:
                with self._db.connect() as conn:
                    row = conn.execute(
                        "SELECT sent_at FROM notification_dedup WHERE channel = ? AND dedup_key = ?",
                        (channel_id, dedup_key),
                    ).fetchone()
                if row is not None:
                    return (time.time() - row["sent_at"]) < self._dedup_window
                return False
            except Exception as exc:  # noqa: BLE001 - a read failure must fall back, not raise
                logger.debug(
                    "notification dedup read failed for %s/%s, falling back to "
                    "in-memory window: %s",
                    channel_id, dedup_key, exc,
                )
                # fall through to the in-memory check below

        # No database configured, or the read above failed: fall back to
        # the in-process window. This dict is always kept current by
        # _record_seen regardless of whether the database is in use (see
        # below), so it is a real fallback rather than always-empty dead
        # code.
        last_seen = self._dedup_seen.get((channel_id, dedup_key))
        if last_seen is None:
            return False
        return (time.monotonic() - last_seen) < self._dedup_window

    def _record_seen(self, channel_id: str, dedup_key: str) -> None:
        # Always update the in-memory fallback, database or not - see
        # _is_duplicate.
        self._dedup_seen[(channel_id, dedup_key)] = time.monotonic()

        if not self._dedup_db_ready():
            return

        now = time.time()
        try:
            with self._db.connect() as conn:
                conn.execute(
                    "INSERT INTO notification_dedup (channel, dedup_key, sent_at) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(channel, dedup_key) DO UPDATE SET sent_at = excluded.sent_at",
                    (channel_id, dedup_key, now),
                )
                # Opportunistic prune, piggybacked on the same write rather
                # than a separate scheduled job - this table only ever
                # grows on a dispatched (non-duplicate) notification, so
                # pruning here on every such write keeps it bounded without
                # its own cron tick. Retention is a multiple of the dedup
                # window (minimum 1 hour): comfortably longer than anything
                # that could still affect dedup, short enough that a
                # channel/error-hash key from a long-resolved incident
                # doesn't sit in the table forever.
                retention_seconds = max(self._dedup_window * 4, 3600.0)
                conn.execute(
                    "DELETE FROM notification_dedup WHERE sent_at < ?",
                    (now - retention_seconds,),
                )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - a write failure must not raise
            logger.debug(
                "notification dedup write failed for %s/%s (dedup for this "
                "call may not persist): %s",
                channel_id, dedup_key, exc,
            )

    # -- Shared push path ---------------------------------------------------

    async def _push(
        self,
        channel_id: str,
        title: str,
        body: str,
        *,
        dedup_key: str,
        url: Optional[str] = None,
        actions: Optional[List[Dict[str, str]]] = None,
    ) -> bool:
        """The single chokepoint every notify_* method funnels through.

        Never raises: every failure mode (disabled, no transport, duplicate,
        network error, non-2xx response) is caught here and turned into a
        ``False`` return, with details (if any) logged rather than surfaced
        to the caller. The sync path must keep working even if notifications
        never do.
        """
        if not self.enabled:
            return False

        try:
            if not self._ensure_transport():
                return False
            if self._is_duplicate(channel_id, dedup_key):
                return False

            payload: Dict[str, Any] = {
                "channel": channel_id,
                "title": title,
                "body": body,
                "group_key": dedup_key,
            }
            if url is not None:
                payload["url"] = url
            if actions:
                payload["actions"] = actions

            dispatched = await self._post("/api/notifications/push", payload)
            if dispatched:
                self._record_seen(channel_id, dedup_key)
            return dispatched
        except Exception as exc:  # noqa: BLE001 - this method's contract is "never raises"
            logger.warning("notification dispatch failed on channel %s: %s", channel_id, exc)
            return False


# -- Module-level singleton --------------------------------------------------

_default_service: Optional[NotificationService] = None


def _resolve_notification_db_path() -> Path:
    """Resolve the sqlite file backing cross-process notification dedup.

    Mirrors ``sync_runner.resolve_db_path()`` - the single resolution
    scheme server.py's HTTP route and cli.py's cron entry point already
    both use for the ``sync_runs``/``conflicts``/``quarantine`` database -
    rather than inventing a second env-var scheme here. Two independent
    schemes could disagree and point notification dedup state at a
    different file than the one runs are actually recorded into, silently
    breaking the whole point of this table.

    Imported lazily, not at module load: ``sync_runner`` imports
    ``get_notification_service`` from *this* module at its own module
    scope, so importing ``sync_runner`` back at this module's top level
    would be a circular import. By the time this function actually runs
    (inside ``get_notification_service()``, i.e. well after both modules
    have finished loading), that's no longer a problem.

    ``resolve_db_path()`` returns ``None`` to mean "no env override, use
    each manager's own default" - for ``Database`` that default is
    ``Path.home() / ".kiro/crew/apps/kirocrew-sync/data/history.db"`` (see
    ``Database.__init__``). That default is recomputed here rather than
    read off a ``Database`` instance so this function has no side effects
    of its own (``Database.__init__`` creates its parent directory as a
    side effect), and ``Path.home()`` is called fresh on every invocation
    rather than cached at import time, so a test that monkeypatches it
    (``conftest.py``'s autouse ``_fake_home`` fixture) is honored even
    though this function typically runs lazily, long after import.
    """
    from .sync_runner import resolve_db_path

    override = resolve_db_path()
    if override is not None:
        return override
    return Path.home() / ".kiro" / "crew" / "apps" / APP_NAME / "data" / "history.db"


def get_notification_service() -> NotificationService:
    """Return the shared NotificationService instance.

    Callers (server.py, sync_manager.py) should use this instead of
    constructing NotificationService directly, so the dedup window,
    transport cache, and (now) the dedup database path are shared across
    the whole process rather than reset per caller.
    """
    global _default_service
    if _default_service is None:
        _default_service = NotificationService(db_path=_resolve_notification_db_path())
    return _default_service
