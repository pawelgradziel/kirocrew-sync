import React, { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardHeader, CardContent, CardFooter } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { RefreshCw, CheckCircle, AlertTriangle, XCircle, Loader2 } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';

interface SyncStatus {
  state: 'idle' | 'syncing' | 'conflict' | 'failed' | 'quarantine';
  last_sync: string | null;
  next_sync: string | null;
  scope: 'personal' | 'team';
  machines_active: number;
  machines_quarantined: number;
  conflicts_pending: number;
}

const stateIcons = {
  idle: CheckCircle,
  syncing: Loader2,
  conflict: AlertTriangle,
  failed: XCircle,
  quarantine: AlertTriangle,
};

const stateColors = {
  idle: 'bg-green-500',
  syncing: 'bg-blue-500',
  conflict: 'bg-yellow-500',
  failed: 'bg-red-500',
  quarantine: 'bg-orange-500',
};

const stateLabels = {
  idle: 'Up to date',
  syncing: 'Syncing...',
  conflict: 'Conflicts',
  failed: 'Failed',
  quarantine: 'Quarantine',
};

interface SyncTriggerResult {
  started: boolean;
  message?: string;
}

export function StatusWidget() {
  const queryClient = useQueryClient();
  const [syncMessage, setSyncMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(
    null
  );

  const { data, isLoading, refetch } = useQuery<{ status: SyncStatus }>({
    queryKey: ['sync-status'],
    queryFn: async () => {
      const response = await fetch('/api/apps/kirocrew-sync/status');
      if (!response.ok) throw new Error('Failed to fetch status');
      return response.json();
    },
    refetchInterval: 10000, // Refresh every 10 seconds
  });

  const triggerSync = useMutation<SyncTriggerResult, Error, void>({
    mutationFn: async () => {
      const response = await fetch('/api/apps/kirocrew-sync/sync', {
        method: 'POST',
      });
      if (!response.ok) throw new Error('Failed to trigger sync');
      return response.json();
    },
    onSuccess: (result) => {
      if (!result.started) {
        setSyncMessage({ type: 'error', text: result.message || 'Sync already in progress' });
        refetch();
        return;
      }

      setSyncMessage(
        result.message ? { type: 'success', text: result.message } : null
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
  const StateIcon = status ? stateIcons[status.state] : CheckCircle;
  const stateColor = status ? stateColors[status.state] : 'bg-gray-500';
  const stateLabel = status ? stateLabels[status.state] : 'Unknown';

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">Sync Status</h3>
          {status && (
            <Badge className={stateColor}>
              <StateIcon className="w-3 h-3 mr-1" />
              {stateLabel}
            </Badge>
          )}
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {isLoading ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-6 h-6 animate-spin" />
          </div>
        ) : status ? (
          <>
            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-sm text-muted-foreground">Last sync</p>
                <p className="text-sm font-medium">
                  {status.last_sync
                    ? formatDistanceToNow(new Date(status.last_sync), { addSuffix: true })
                    : 'Never'}
                </p>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Next sync</p>
                <p className="text-sm font-medium">
                  {status.next_sync
                    ? formatDistanceToNow(new Date(status.next_sync), { addSuffix: true })
                    : 'Not scheduled'}
                </p>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <p className="text-sm text-muted-foreground">Scope</p>
                <Badge variant="outline">{status.scope}</Badge>
              </div>
              <div>
                <p className="text-sm text-muted-foreground">Machines</p>
                <p className="text-sm font-medium">
                  {status.machines_active} active
                  {status.machines_quarantined > 0 && (
                    <span className="text-orange-500">
                      {' '}
                      / {status.machines_quarantined} quarantined
                    </span>
                  )}
                </p>
              </div>
            </div>

            {status.conflicts_pending > 0 && (
              <div className="p-3 bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-md">
                <p className="text-sm text-yellow-800 dark:text-yellow-200">
                  <AlertTriangle className="w-4 h-4 inline mr-1" />
                  {status.conflicts_pending} conflict{status.conflicts_pending > 1 ? 's' : ''} need
                  resolution
                </p>
              </div>
            )}

            {syncMessage && (
              <div
                className={
                  syncMessage.type === 'success'
                    ? 'p-2 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-md'
                    : 'p-2 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md'
                }
              >
                <p
                  className={
                    syncMessage.type === 'success'
                      ? 'text-sm text-green-800 dark:text-green-200 flex items-center gap-1'
                      : 'text-sm text-red-800 dark:text-red-200 flex items-center gap-1'
                  }
                >
                  {syncMessage.type === 'success' ? (
                    <CheckCircle className="w-4 h-4 shrink-0" />
                  ) : (
                    <XCircle className="w-4 h-4 shrink-0" />
                  )}
                  {syncMessage.text}
                </p>
              </div>
            )}
          </>
        ) : (
          <p className="text-sm text-muted-foreground">Failed to load status</p>
        )}
      </CardContent>

      <CardFooter>
        <Button
          onClick={() => {
            setSyncMessage(null);
            triggerSync.mutate();
          }}
          disabled={triggerSync.isPending || status?.state === 'syncing'}
          className="w-full"
        >
          {triggerSync.isPending ? (
            <>
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              Syncing...
            </>
          ) : (
            <>
              <RefreshCw className="w-4 h-4 mr-2" />
              Sync Now
            </>
          )}
        </Button>
      </CardFooter>
    </Card>
  );
}
