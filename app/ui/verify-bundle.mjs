#!/usr/bin/env node
// Guard against the class of bug that broke Crew Sync in KiroCrew: a bare
// module specifier that esbuild left as an external `import` statement (see
// esbuild.config.mjs's SHARED_MODULES / `external` list) but that the host
// does not actually serve, or a named import of something a vendor stub
// doesn't actually export. Both fail at ESM module resolution / link time in
// the browser — not at build time — and a single bad specifier or missing
// named export kills the ENTIRE bundle (an ES module can't partially load),
// exactly what happened with `@kirocrew/ui` and would have happened again
// with lucide-react icons like `CheckCircle` that the host's stub doesn't
// destructure.
//
// This script parses dist/index.mjs after every build and re-derives, from
// the bundle's own top-level `import` statements, whether that failure mode
// is still possible:
//   1. every bare specifier must be a key in the host's served import map
//   2. every NAMED import from a module backed by a vendor stub must be a
//      name that stub actually exports
//
// Run automatically as part of `npm run build` (see package.json). Exits
// non-zero on any violation.
//
// ---------------------------------------------------------------------------
// GROUND TRUTH — hardcoded from a running KiroCrew build. Re-check both
// tables below against a fresh build if this guard ever fails for a reason
// that looks like the HOST changed rather than our code:
//   import map:   <script type="importmap"> in the served index.html at
//     <mounted-appimage>/resources/backend-dist/kirocrew-backend/lib/python3.12/
//     site-packages/kiro_crew/static/dist/index.html
//   vendor stubs: same dist/ directory's vendor/*.mjs files
// Last verified: 2026-08-12.
// ---------------------------------------------------------------------------

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const bundlePath = process.argv[2] ? join(process.cwd(), process.argv[2]) : join(__dirname, 'dist/index.mjs');

// Every specifier the host's import map resolves. A bare `import` of
// anything NOT in this set fails with "Failed to resolve module specifier"
// exactly like `@kirocrew/ui` did.
const HOST_IMPORT_MAP = new Set([
  'react',
  'react-dom',
  'react-dom/client',
  'react/jsx-runtime',
  '@kirocrew/app-sdk',
  '@kirocrew/app-sdk/ui',
  'lucide-react',
]);

// For each host-served specifier: the exact set of names its vendor stub
// destructures (plus 'default' where the stub has a default export). A
// static named import of anything outside this set type-checks (nothing
// enforces it locally) but crashes at link time in the browser, taking the
// whole bundle down with it.
const VENDOR_STUB_EXPORTS = {
  react: new Set([
    'default',
    'useState', 'useEffect', 'useRef', 'useCallback', 'useMemo', 'useContext', 'useReducer',
    'useLayoutEffect', 'useImperativeHandle', 'useDebugValue', 'useDeferredValue',
    'useTransition', 'useId', 'useSyncExternalStore', 'useInsertionEffect',
    'createContext', 'createElement', 'cloneElement', 'createRef', 'forwardRef',
    'lazy', 'memo', 'startTransition', 'Fragment', 'Suspense', 'StrictMode',
    'Children', 'Component', 'PureComponent', 'isValidElement',
  ]),
  'react-dom': new Set(['default', 'createPortal', 'flushSync', 'unstable_batchedUpdates']),
  'react-dom/client': new Set(['createRoot', 'hydrateRoot']),
  'react/jsx-runtime': new Set(['jsx', 'jsxs', 'jsxDEV', 'Fragment']),
  '@kirocrew/app-sdk': new Set([
    'useAppApi', 'useAppEvents', 'useTheme', 'useAppInfo', 'useNavigate', 'useNotify',
    'useNavBadge', 'useChatLauncher', 'AppApiProvider',
  ]),
  '@kirocrew/app-sdk/ui': new Set([
    'Card', 'CardTitle', 'Btn', 'SendBtn', 'Input', 'SearchInput',
    'Badge', 'AimBadge', 'StatCard', 'Skeleton', 'ContentSkeleton',
    'EmptyState', 'PageHeader', 'Toggle', 'InfoTip', 'SegmentedControl',
    'MarkdownRenderer',
  ]),
  'lucide-react': new Set([
    'default', // Proxy forwarding any icon name — always safe
    'AlertTriangle', 'ArrowLeft', 'ArrowRight', 'ArrowUp', 'Bell', 'Bot', 'Brain', 'Building2',
    'Calendar', 'Check', 'ChevronRight', 'Clock', 'Code', 'Download', 'ExternalLink',
    'Gamepad2', 'Heart', 'Home', 'Loader2', 'Menu', 'MessageSquare', 'Moon', 'Package',
    'Plug', 'Plus', 'Power', 'RefreshCw', 'Rocket', 'Search', 'Settings', 'Shield', 'Sparkles',
    'Star', 'Sun', 'Tag', 'Trash2', 'Users', 'Wand2', 'Waves', 'X', 'Zap',
  ]),
};

// --- parse ------------------------------------------------------------------

let source;
try {
  source = readFileSync(bundlePath, 'utf8');
} catch (err) {
  console.error(`[verify-bundle] Could not read ${bundlePath}: ${err.message}`);
  process.exit(1);
}

// Matches static `import <clause> from "<specifier>"` statements. The
// clause can't contain ';' in valid JS/esbuild output, which is what keeps
// this from over-matching across statements in a minified one-line bundle.
const IMPORT_RE = /import\s*([^;]*?)from\s*["']([^"']+)["']/g;

function parseClause(rawClause) {
  const clause = rawClause.trim();
  if (clause.startsWith('*')) {
    return { namespace: true, defaultName: null, named: [] };
  }
  const namedMatch = clause.match(/\{([^}]*)\}/);
  const named = namedMatch
    ? namedMatch[1]
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
        .map((part) => {
          const asMatch = part.match(/^([\w$]+)\s+as\s+([\w$]+)$/);
          return asMatch ? { imported: asMatch[1], local: asMatch[2] } : { imported: part, local: part };
        })
    : [];
  const beforeBrace = clause.split('{')[0].replace(/,\s*$/, '').trim();
  const defaultName = beforeBrace || null;
  return { namespace: false, defaultName, named };
}

const imports = [];
for (const match of source.matchAll(IMPORT_RE)) {
  const [, rawClause, specifier] = match;
  imports.push({ specifier, ...parseClause(rawClause) });
}

if (imports.length === 0) {
  console.error(`[verify-bundle] Found zero top-level import statements in ${bundlePath} — parser or build is broken.`);
  process.exit(1);
}

// --- verify -------------------------------------------------------------

const violations = [];

for (const imp of imports) {
  if (!HOST_IMPORT_MAP.has(imp.specifier)) {
    violations.push(
      `Bare specifier "${imp.specifier}" is not in the host's import map. ` +
        `This is exactly the "@kirocrew/ui" failure mode — it will fail with ` +
        `"Failed to resolve module specifier" at runtime. ` +
        `Host import map: ${[...HOST_IMPORT_MAP].join(', ')}`
    );
    continue;
  }

  const allowedExports = VENDOR_STUB_EXPORTS[imp.specifier];

  if (imp.defaultName && !allowedExports.has('default')) {
    violations.push(
      `"${imp.specifier}" is imported with a default import, but its vendor stub has no default export.`
    );
  }

  for (const { imported } of imp.named) {
    if (!allowedExports.has(imported)) {
      violations.push(
        `Named import "${imported}" from "${imp.specifier}" is not exported by that module's vendor ` +
          `stub. This is a link-time crash that takes down the whole bundle — an ES module can't ` +
          `partially load. Stub exports: ${[...allowedExports].join(', ')}`
      );
    }
  }
}

if (violations.length > 0) {
  console.error(`[verify-bundle] FAILED — ${violations.length} violation(s) in ${bundlePath}:\n`);
  for (const v of violations) console.error(`  - ${v}`);
  console.error('');
  process.exit(1);
}

console.log(
  `[verify-bundle] OK — ${imports.length} top-level import statement(s) checked against the host's ` +
    `import map and vendor stub exports. ${bundlePath}`
);
