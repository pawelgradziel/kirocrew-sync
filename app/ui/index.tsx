// Entry module for the Crew Sync dashboard bundle.
//
// AppHost dynamically imports this file's BUILT output (dist/index.mjs, per
// ui.entry in app.json) and passes its default export to React lazy() — see
// AppHost.tsx in the KiroCrew frontend. React, ReactDOM, the JSX runtime,
// lucide-react, @kirocrew/app-sdk and @kirocrew/app-sdk/ui are all resolved
// through the host's import map (see esbuild.config.mjs's `external` list)
// rather than bundled, so this app shares the host's exact module instances
// instead of shipping a second React.
//
// @tanstack/react-query is different: it is NOT in the host's import map (see
// esbuild.config.mjs), so it is bundled into this file instead of shared.
// That's safe — react-query's client/context living inside our own bundle
// doesn't create a second React instance, only a second (private) query
// cache, which is exactly what we want since AppHost.tsx's provider chain
// (Suspense -> AppErrorBoundary -> AppApiProvider -> Suspense -> LazyApp,
// see website/src/components/AppHost.tsx) does not wrap app bundles in any
// QueryClientProvider of its own. Without one here, every useQuery/
// useMutation call in the dashboard components would throw "No QueryClient
// set" the instant it mounted. The QueryClient is created once at module
// scope so it survives re-renders and dev-mode retries (AppHost bumps a
// resetKey and remounts LazyApp on retry) without losing cached data.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import SyncDashboard from './components/SyncDashboard';

const queryClient = new QueryClient();

export default function CrewSyncApp() {
  return (
    <QueryClientProvider client={queryClient}>
      <SyncDashboard />
    </QueryClientProvider>
  );
}
