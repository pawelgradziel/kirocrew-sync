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
    """

    def __init__(self, enabled: bool = True, dedup_window_seconds: float = DEFAULT_DEDUP_WINDOW_SECONDS) -> None:
        """
        Args:
            enabled: Master on/off switch. When False, every notify_* call
                is a no-op (still returns False, never raises).
            dedup_window_seconds: How long a (channel, dedup-key) pair
                suppresses a repeat notification. Injectable so tests don't
                have to sleep for 5 minutes; defaults to the app's cron
                cadence (see DEFAULT_DEDUP_WINDOW_SECONDS).
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

    def _is_duplicate(self, channel_id: str, dedup_key: str) -> bool:
        last_seen = self._dedup_seen.get((channel_id, dedup_key))
        if last_seen is None:
            return False
        return (time.monotonic() - last_seen) < self._dedup_window

    def _record_seen(self, channel_id: str, dedup_key: str) -> None:
        self._dedup_seen[(channel_id, dedup_key)] = time.monotonic()

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


def get_notification_service() -> NotificationService:
    """Return the shared NotificationService instance.

    Callers (server.py, sync_manager.py) should use this instead of
    constructing NotificationService directly, so the dedup window and
    transport cache are shared across the whole process rather than reset
    per caller.
    """
    global _default_service
    if _default_service is None:
        _default_service = NotificationService()
    return _default_service
