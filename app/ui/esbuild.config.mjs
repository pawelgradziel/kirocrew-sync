// Builds the Crew Sync dashboard into a single ESM bundle at dist/index.mjs.
//
// This is what AppHost.tsx loads via `import('/apps/kirocrew-sync/ui/index.mjs')`
// (see ui.entry in ../app.json) and passes to React lazy(), so the entry
// module's default export must be a React component — see ../index.tsx.
//
// Only modules present in the HOST'S SERVED IMPORT MAP (see the <script
// type="importmap"> in a running KiroCrew build's index.html, and
// verify-bundle.mjs's HOST_IMPORT_MAP below, which is the ground truth this
// list must match) may be marked external. The host's import map resolves
// these specifiers to its own already-running React/ReactDOM/etc. instances
// at runtime; bundling any of them here would create a second React
// instance, which breaks hooks (shared-modules.ts says so explicitly).
// external + format:'esm' is what keeps them as real top-level `import`
// statements in the output instead of inlined code — verify with
// `head -20 dist/index.mjs`.
//
// @tanstack/react-query is deliberately NOT in this list: it is absent from
// the host's import map, so marking it external would produce a bare
// `import ... from "@tanstack/react-query"` that fails to resolve exactly
// like the old `@kirocrew/ui` specifier did. It is bundled instead (see
// index.tsx, which wraps the dashboard in its own QueryClientProvider).
import { build } from 'esbuild';

const SHARED_MODULES = [
  'react',
  'react-dom',
  'react/jsx-runtime',
  'lucide-react',
  '@kirocrew/app-sdk',
  '@kirocrew/app-sdk/ui',
];

await build({
  entryPoints: ['index.tsx'],
  outfile: 'dist/index.mjs',
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'es2020',
  jsx: 'automatic',
  external: SHARED_MODULES,
  minify: true,
  sourcemap: false,
  logLevel: 'info',
});
