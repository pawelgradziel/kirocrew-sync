/**
 * Tiny relative/absolute time formatting, standing in for the two date-fns
 * helpers (`formatDistanceToNow`, `format`) the original shadcn-based
 * components used.
 *
 * date-fns is NOT one of the host's shared modules (see
 * website/src/app-sdk/shared-modules.ts), so pulling it in would bundle an
 * entire formatting library — locale data included — into this app just for
 * two calls. Intl.RelativeTimeFormat and Intl.DateTimeFormat are built into
 * every browser KiroCrew runs in, so this gets the same UX for zero bytes.
 */

const RELATIVE_UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ['year', 31536000],
  ['month', 2592000],
  ['week', 604800],
  ['day', 86400],
  ['hour', 3600],
  ['minute', 60],
  ['second', 1],
]

const relativeFormatter = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })

/** e.g. "3 minutes ago" / "in 3 minutes" — mirrors date-fns'
 * `formatDistanceToNow(date, { addSuffix: true })`. */
export function formatRelativeTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  const diffSec = (date.getTime() - Date.now()) / 1000
  const absSec = Math.abs(diffSec)

  for (const [unit, secInUnit] of RELATIVE_UNITS) {
    if (absSec >= secInUnit || unit === 'second') {
      return relativeFormatter.format(Math.round(diffSec / secInUnit), unit)
    }
  }
  return relativeFormatter.format(0, 'second')
}

const dateFormatter = new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
const timeFormatter = new Intl.DateTimeFormat('en-US', {
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

/** e.g. "Aug 12, 2026 14:03:05" — mirrors date-fns' `format(date, 'MMM d, yyyy HH:mm:ss')`. */
export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return `${dateFormatter.format(date)} ${timeFormatter.format(date)}`
}
