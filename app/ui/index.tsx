// Entry module for the Crew Sync dashboard bundle.
//
// AppHost dynamically imports this file's BUILT output (dist/index.mjs, per
// ui.entry in app.json) and passes its default export to React lazy() — see
// AppHost.tsx in the KiroCrew frontend. React, ReactDOM, the JSX runtime,
// lucide-react, @tanstack/react-query, @kirocrew/app-sdk and @kirocrew/ui are
// all resolved through the host's import map (see esbuild.config.mjs's
// `external` list) rather than bundled, so this app shares the host's exact
// module instances instead of shipping a second React.
export { default } from './components/SyncDashboard';
