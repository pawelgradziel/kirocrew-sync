import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardTitle, Btn, Badge } from '@kirocrew/app-sdk/ui';
import lucideIcons from 'lucide-react';
import { ErrorBlock, Message } from './shared';
import { formatRelativeTime } from '../lib/time';
import { apiFetchJson } from '../lib/api';

const { RefreshCw, CheckCircle, AlertTriangle, XCircle, ShieldAlert, Loader2 } = lucideIcons;

interface SyncStatus {
  state: 'idle' | 'syncing' | 'conflict' | 'failed' | 'quarantine';
  last_sync: string | null;
  next_sync: string | null;
  scope: 'personal' | 'team';
  machines_active: number;
  machines_quarantined: number;
  conflicts_pending: number;
}

const STATE_ICON: Record<SyncStatus['state'], typeof CheckCircle> = {
  idle: CheckCircle,
  syncing: Loader2,
  conflict: AlertTriangle,
  failed: XCircle,
  quarantine: ShieldAlert,
};

const STATE_VARIANT: Record<SyncStatus['state'], 'ok' | 'err' | 'warn' | 'aim'> = {
  idle: 'ok',
  syncing: 'aim',
  conflict: 'warn',
  failed: 'err',
  quarantine: 'err',
};

const STATE_LABEL: Record<SyncStatus['state'], string> = {
  idle: 'Up to date',
  syncing: 'Syncing…',
  conflict: 'Conflicts',
  failed: 'Failed',
  quarantine: 'Quarantine',
};

interface SyncTriggerResult {
  /** The engine was actually launched (false only for the already-running short-circuit). */
  started: boolean;
  /** Whether the sync itself succeeded. `started` says a run happened, NOT that it worked --
   *  conflating the two is what previously rendered "Sync completed with exit code 1" in a
   *  green success banner right next to a red "Failed" status badge. */
  success?: boolean;
  /** Engine exit code: 0 ok, 3 completed-with-quarantine (also a success), 1 failed. */
  exit_code?: number | null;
  message?: string;
}

export function StatusWidget() {
  const queryClient = useQueryClient();
  const [syncMessage, setSyncMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(
    null
  );

  const { data, isLoading, error, refetch } = useQuery<{ status: SyncStatus }>({
    queryKey: ['sync-status'],
    queryFn: () => apiFetchJson('/status'),
    refetchInterval: 10000, // Refresh every 10 seconds
  });

  const triggerSync = useMutation<SyncTriggerResult, Error, void>({
    mutationFn: () => apiFetchJson('/sync', { method: 'POST' }),
    onSuccess: (result) => {
      if (!result.started) {
        setSyncMessage({ type: 'error', text: result.message || 'Sync already in progress' });
        refetch();
        return;
      }

      // Style on `success`, not `started`. A run that launched and then failed is
      // still a failure, and older builds reported it in a green banner that
      // directly contradicted the red status badge beside it. `success` is
      // optional on the wire, so fall back to the exit-code contract (0 and 3
      // are both successes) and only then to "it started".
      const ok =
        result.success ??
        (result.exit_code == null ? true : [0, 3].includes(result.exit_code));
      setSyncMessage(
        result.message ? { type: ok ? 'success' : 'error', text: result.message } : null
      );
      refetch();
      queryClient.invalidateQueries({ queryKey: ['sync-history'] });
      queryClient.invalidateQueries({ queryKey: ['conflicts'] });
      queryClient.invalidateQueries({ queryKey: ['quarantine'] });
    },
    onError: (error) => {
      setSyncMessage({ type: 'error', text: error.message });
    },
  });

  const status = data?.status;
  const StateIcon = status ? STATE_ICON[status.state] : CheckCircle;

  return (
    <Card>
      <div className="flex items-center justify-between mb-4">
        <CardTitle className="mb-0">Sync Status</CardTitle>
        {status && (
          <Badge variant={STATE_VARIANT[status.state]}>
            <StateIcon size={12} className={status.state === 'syncing' ? 'animate-spin' : undefined} />
            {STATE_LABEL[status.state]}
          </Badge>
        )}
      </div>

      {isLoading ? (
        <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted">
          <Loader2 size={16} className="animate-spin" />
          Loading status…
        </div>
      ) : status ? (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="text-xs text-muted mb-1">Last sync</p>
              <p className="text-sm font-medium text-text">
                {status.last_sync ? formatRelativeTime(status.last_sync) : 'Never'}
              </p>
            </div>
            <div>
              <p className="text-xs text-muted mb-1">Next sync</p>
              <p className="text-sm font-medium text-text">
                {status.next_sync ? formatRelativeTime(status.next_sync) : 'Not scheduled'}
              </p>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="text-xs text-muted mb-1">Scope</p>
              <Badge variant="muted">{status.scope}</Badge>
            </div>
            <div>
              <p className="text-xs text-muted mb-1">Machines</p>
              <p className="text-sm font-medium text-text">
                {status.machines_active} active
                {status.machines_quarantined > 0 && (
                  <span className="text-warn"> / {status.machines_quarantined} quarantined</span>
                )}
              </p>
            </div>
          </div>

          {status.conflicts_pending > 0 && (
            <div className="bg-warn-subtle text-warn rounded-md p-3 flex items-center gap-2 text-sm">
              <AlertTriangle size={14} className="shrink-0" />
              {status.conflicts_pending} conflict{status.conflicts_pending > 1 ? 's' : ''} need
              resolution
            </div>
          )}

          {syncMessage && <Message tone={syncMessage.type}>{syncMessage.text}</Message>}
        </div>
      ) : (
        <ErrorBlock label="Failed to load status" detail={error?.message} onRetry={() => refetch()} />
      )}

      <Btn
        primary
        onClick={() => {
          setSyncMessage(null);
          triggerSync.mutate();
        }}
        disabled={triggerSync.isPending || status?.state === 'syncing'}
        className="w-full justify-center mt-4"
      >
        {triggerSync.isPending ? (
          <>
            <Loader2 size={14} className="animate-spin" />
            Syncing…
          </>
        ) : (
          <>
            <RefreshCw size={14} />
            Sync Now
          </>
        )}
      </Btn>
    </Card>
  );
}
