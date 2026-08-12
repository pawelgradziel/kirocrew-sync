// Ambient type declarations for lucide-react as resolved through the host's
// import map at runtime (see ../esbuild.config.mjs and /vendor/lucide-react.mjs
// in the running KiroCrew build — kiro_crew/static/dist/vendor/lucide-react.mjs).
//
// The REAL npm lucide-react package (installed only as dev tooling, never
// bundled — see SHARED_MODULES in ../esbuild.config.mjs) exports hundreds of
// named icon components and has NO default export. The host's vendor stub is
// different: it exports a fixed list of ~39 named icons PLUS a default export
// that is a Proxy forwarding any icon name to the host's real lucide-react
// instance. A static `import { X } from 'lucide-react'` for any name outside
// that fixed list type-checks fine against the npm package but is a
// link-time crash against the stub (an ES module can't partially fail to
// load — one missing named export kills the whole app bundle). We declare
// types against the STUB's actual shape instead, via the tsconfig `paths`
// override below, so that mistake is caught here at typecheck time too.
//
// This app therefore imports the DEFAULT export only and destructures icons
// off it (see components/*.tsx) — that always resolves through the stub's
// Proxy regardless of which icon name is used, so it cannot regress when
// someone adds an icon later. Re-check this list against a new KiroCrew
// build if bundle loading breaks again.
declare module 'lucide-react' {
  import type { ForwardRefExoticComponent, RefAttributes, SVGProps } from 'react';

  export type LucideIcon = ForwardRefExoticComponent<
    Omit<SVGProps<SVGSVGElement>, 'ref'> & { size?: string | number } & RefAttributes<SVGSVGElement>
  >;

  export const AlertTriangle: LucideIcon;
  export const ArrowLeft: LucideIcon;
  export const ArrowRight: LucideIcon;
  export const ArrowUp: LucideIcon;
  export const Bell: LucideIcon;
  export const Bot: LucideIcon;
  export const Brain: LucideIcon;
  export const Building2: LucideIcon;
  export const Calendar: LucideIcon;
  export const Check: LucideIcon;
  export const ChevronRight: LucideIcon;
  export const Clock: LucideIcon;
  export const Code: LucideIcon;
  export const Download: LucideIcon;
  export const ExternalLink: LucideIcon;
  export const Gamepad2: LucideIcon;
  export const Heart: LucideIcon;
  export const Home: LucideIcon;
  export const Loader2: LucideIcon;
  export const Menu: LucideIcon;
  export const MessageSquare: LucideIcon;
  export const Moon: LucideIcon;
  export const Package: LucideIcon;
  export const Plug: LucideIcon;
  export const Plus: LucideIcon;
  export const Power: LucideIcon;
  export const RefreshCw: LucideIcon;
  export const Rocket: LucideIcon;
  export const Search: LucideIcon;
  export const Settings: LucideIcon;
  export const Shield: LucideIcon;
  export const Sparkles: LucideIcon;
  export const Star: LucideIcon;
  export const Sun: LucideIcon;
  export const Tag: LucideIcon;
  export const Trash2: LucideIcon;
  export const Users: LucideIcon;
  export const Wand2: LucideIcon;
  export const Waves: LucideIcon;
  export const X: LucideIcon;
  export const Zap: LucideIcon;

  // The stub's actual escape hatch: a Proxy over the host's full icon set.
  // Typed as an index signature so any icon name can be destructured off it
  // — this is the ONLY import form this app should use (see note above).
  const icons: Record<string, LucideIcon>;
  export default icons;
}
