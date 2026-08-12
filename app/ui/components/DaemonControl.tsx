import React, { useEffect, useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Slider } from '@/components/ui/slider';
import { CheckCircle, XCircle, RotateCw, Loader2 } from 'lucide-react';

interface DaemonConfig {
  enabled: boolean;
  scope: 'personal' | 'team';
  interval: number;
}

interface DaemonControlResult {
  success: boolean;
  action: string;
  message?: string;
}

type ControlAction = 'start' | 'stop' | 'restart';

async function controlDaemon(action: ControlAction): Promise<DaemonControlResult> {
  const response = await fetch('/api/apps/kirocrew-sync/daemon/control', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  });
  if (!response.ok) throw new Error(`Failed to ${action} daemon`);
  return response.json();
}

export function DaemonControl() {
  const [controlMessage, setControlMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(
    null
  );
  // Local, in-flight value for the interval slider so dragging is smooth and
  // isn't fought by the server round-trip - only persisted on commit.
  const [intervalValue, setIntervalValue] = useState<number | null>(null);

  const {
    data,
    isLoading,
    isError,
    refetch,
  } = useQuery<DaemonConfig>({
    queryKey: ['daemon-config'],
    queryFn: async () => {
      const response = await fetch('/api/apps/kirocrew-sync/daemon/config');
      if (!response.ok) throw new Error('Failed to fetch config');
      return response.json();
    },
  });

  useEffect(() => {
    if (data) setIntervalValue(data.interval);
  }, [data?.interval]);

  const updateConfig = useMutation({
    mutationFn: async (config: Partial<DaemonConfig>) => {
      const response = await fetch('/api/apps/kirocrew-sync/daemon/config', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ...data, ...config }),
      });
      if (!response.ok) throw new Error('Failed to update config');
      return response.json();
    },
    onSuccess: () => {
      refetch();
    },
    onError: (error: Error) => {
      setControlMessage({ type: 'error', text: error.message });
    },
  });

  // Flipping the "Background sync" switch only needs to drive the real
  // start/stop of the daemon process - the server persists the `enabled`
  // flag itself once the start/stop actually succeeds (see server.py's
  // /daemon/control handler), so a separate pre-emptive PUT here would only
  // risk leaving the config row out of sync with reality when start/stop
  // fails.
  const toggleBackgroundSync = useMutation<DaemonControlResult, Error, boolean>({
    mutationFn: async (enabled) => controlDaemon(enabled ? 'start' : 'stop'),
    onSuccess: (result) => {
      setControlMessage(
        result.success
          ? null
          : { type: 'error', text: result.message || `Failed to ${result.action} daemon` }
      );
      refetch();
    },
    onError: (error) => {
      setControlMessage({ type: 'error', text: error.message });
    },
  });

  const restartDaemon = useMutation<DaemonControlResult, Error, void>({
    mutationFn: () => controlDaemon('restart'),
    onSuccess: (result) => {
      setControlMessage(
        result.success
          ? { type: 'success', text: result.message || 'Daemon restarted' }
          : { type: 'error', text: result.message || 'Failed to restart daemon' }
      );
      refetch();
    },
    onError: (error) => {
      setControlMessage({ type: 'error', text: error.message });
    },
  });

  if (isLoading) {
    return (
      <Card>
        <CardHeader>
          <h3 className="text-lg font-semibold">Daemon Control</h3>
        </CardHeader>
        <CardContent className="py-8 text-center">
          <div className="flex items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="w-4 h-4 animate-spin" />
            Loading daemon status...
          </div>
        </CardContent>
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card>
        <CardHeader>
          <h3 className="text-lg font-semibold">Daemon Control</h3>
        </CardHeader>
        <CardContent className="py-8 text-center space-y-3">
          <p className="text-sm text-muted-foreground">Failed to load daemon status</p>
          <Button size="sm" variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const isBusy = toggleBackgroundSync.isPending || restartDaemon.isPending;

  return (
    <Card>
      <CardHeader>
        <h3 className="text-lg font-semibold">Daemon Control</h3>
      </CardHeader>

      <CardContent className="space-y-6">
        <div className="flex items-center justify-between">
          <Label htmlFor="daemon-enabled" className="text-sm font-medium">
            Background sync
          </Label>
          <Switch
            id="daemon-enabled"
            checked={data.enabled}
            disabled={isBusy}
            onCheckedChange={(enabled) => toggleBackgroundSync.mutate(enabled)}
          />
        </div>

        {controlMessage && (
          <div
            className={
              controlMessage.type === 'success'
                ? 'p-2 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-md'
                : 'p-2 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md'
            }
          >
            <p
              className={
                controlMessage.type === 'success'
                  ? 'text-sm text-green-800 dark:text-green-200 flex items-center gap-1'
                  : 'text-sm text-red-800 dark:text-red-200 flex items-center gap-1'
              }
            >
              {controlMessage.type === 'success' ? (
                <CheckCircle className="w-4 h-4 shrink-0" />
              ) : (
                <XCircle className="w-4 h-4 shrink-0" />
              )}
              {controlMessage.text}
            </p>
          </div>
        )}

        <Button
          size="sm"
          variant="outline"
          onClick={() => restartDaemon.mutate()}
          disabled={isBusy}
          className="w-full"
        >
          {restartDaemon.isPending ? (
            <>
              <Loader2 className="w-4 h-4 mr-2 animate-spin" />
              Restarting...
            </>
          ) : (
            <>
              <RotateCw className="w-4 h-4 mr-2" />
              Restart daemon
            </>
          )}
        </Button>

        <div className="space-y-2">
          <Label htmlFor="sync-scope" className="text-sm font-medium">
            Scope
          </Label>
          <Select
            value={data.scope}
            onValueChange={(scope: 'personal' | 'team') => updateConfig.mutate({ scope })}
          >
            <SelectTrigger id="sync-scope">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="personal">Personal</SelectItem>
              <SelectItem value="team">Team</SelectItem>
            </SelectContent>
          </Select>
          <p className="text-xs text-muted-foreground">
            {data.scope === 'personal'
              ? 'Sync all data across your own machines'
              : 'Share knowledge with your team (excludes transcripts)'}
          </p>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <Label htmlFor="sync-interval" className="text-sm font-medium">
              Interval
            </Label>
            <span className="text-sm text-muted-foreground">
              {Math.floor((intervalValue ?? data.interval) / 60)} minutes
            </span>
          </div>
          <Slider
            id="sync-interval"
            min={60}
            max={900}
            step={60}
            value={[intervalValue ?? data.interval]}
            onValueChange={([interval]) => setIntervalValue(interval)}
            onValueCommit={([interval]) => updateConfig.mutate({ interval })}
            className="w-full"
          />
          <p className="text-xs text-muted-foreground">
            How often to check for changes (1-15 minutes)
          </p>
        </div>
      </CardContent>
    </Card>
  );
}
