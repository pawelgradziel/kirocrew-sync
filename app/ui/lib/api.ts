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

const MAX_ERROR_BODY_CHARS = 300

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

/**
 * fetch() wrapper used by every component that talks to this app's backend
 * — nothing should call the global `fetch` directly. Before this, a failed
 * panel fetch just threw a made-up string like `new Error('Failed to fetch
 * status')`: no status code, no server message, no way to tell whether the
 * request even reached the backend. This throws an Error that always
 * carries three things instead:
 *
 *  1. The HTTP status and the server's own response body (truncated), e.g.
 *     `HTTP 404: {"detail":"Not Found"} (GET /apps/kirocrew-sync/api/nope)`
 *     — for a non-ok (non 2xx) response.
 *  2. A distinct "network error" label for the case fetch() itself rejects
 *     (backend unreachable, DNS failure, CORS, offline, stale service
 *     worker...) — a bare `TypeError` from fetch means the request never
 *     got a response at all, which is a fundamentally different failure
 *     from a 404/500 and must not be presented the same way.
 *  3. The exact URL that was requested, in both cases — if the browser is
 *     running a stale cached bundle hitting an old/wrong path, seeing the
 *     actual URL in the error is what makes that obvious.
 */
export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const url = `${API_BASE}${path}`
  const method = init?.method ?? 'GET'

  let response: Response
  try {
    response = await fetch(url, init)
  } catch (err) {
    // fetch() rejects (rather than resolving with a non-ok Response) only
    // when no response was ever received — a TypeError in every browser
    // that implements the spec. Relabeled here rather than left as
    // whatever generic message the browser gives ("Failed to fetch" /
    // "Load failed" / "NetworkError when attempting to fetch resource"),
    // so it reads as "the request never arrived" instead of looking like
    // any other Error a caller might catch.
    const cause = err instanceof Error ? err.message : String(err)
    throw new Error(`Network error (no response received) for ${method} ${url}: ${cause}`)
  }

  if (!response.ok) {
    let bodyText = ''
    try {
      bodyText = await response.text()
    } catch {
      // Response body unreadable (already consumed, stream error, ...) —
      // fall through with an empty snippet rather than losing the status.
    }
    const snippet = bodyText ? truncate(bodyText, MAX_ERROR_BODY_CHARS) : '(empty response body)'
    throw new Error(`HTTP ${response.status}: ${snippet} (${method} ${url})`)
  }

  return response
}

/** apiFetch() + response.json(), for the common case of a JSON API response. */
export async function apiFetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init)
  return response.json() as Promise<T>
}
