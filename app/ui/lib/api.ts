/**
 * Base path for every fetch this app makes to its own backend.
 *
 * KiroCrew's dashboard reverse proxy (handle_app_api_proxy in the gateway's
 * src/kiro_crew/apps/routes.py) is registered at
 * `/apps/{name}/api/{path:.*}` and forwards same-origin browser requests to
 * this app's backend, re-adding the `/api/` prefix on the way through so the
 * backend (backend/server.py, whose routes live on an `APIRouter(prefix=
 * "/api")`) sees its own `/api/...` paths. `/api/apps/kirocrew-sync/...` is
 * a DIFFERENT route entirely — the gateway's in-process app-management API,
 * which has no handlers registered for this app's own endpoints and 404s.
 *
 * Defined once and imported everywhere a component talks to the backend, so
 * the base path can't drift per-component the way it did before (every
 * panel independently hardcoded the wrong `/api/apps/kirocrew-sync` prefix).
 * Must stay in sync with `permissions.api` in app.json, which is what the
 * app-sdk's `createScopedApi` allowlists fetches against in the browser.
 */
export const API_BASE = '/apps/kirocrew-sync/api'
