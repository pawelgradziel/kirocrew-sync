// Builds the Crew Sync dashboard into a single ESM bundle at dist/index.mjs.
//
// This is what AppHost.tsx loads via `import('/apps/kirocrew-sync/ui/index.mjs')`
// (see ui.entry in ../app.json) and passes to React lazy(), so the entry
// module's default export must be a React component — see ../index.tsx.
//
// EVERY module the host shares (website/src/app-sdk/shared-modules.ts) is
// marked external below. The host's import map resolves these specifiers to
// its own already-running React/ReactDOM/etc. instances at runtime; bundling
// any of them here would create a second React instance, which breaks hooks
// (shared-modules.ts says so explicitly). external + format:'esm' is what
// keeps them as real top-level `import` statements in the output instead of
// inlined code — verify with `head -20 dist/index.mjs`.
import { build } from 'esbuild';

const SHARED_MODULES = [
  'react',
  'react-dom',
  'react/jsx-runtime',
  'lucide-react',
  '@tanstack/react-query',
  '@kirocrew/app-sdk',
  '@kirocrew/ui',
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
