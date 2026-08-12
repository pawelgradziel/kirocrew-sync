import { useEffect, useState } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { Card, CardTitle, Btn, Toggle } from '@kirocrew/ui';
import { RotateCw, Loader2 } from 'lucide-react';
import { ErrorBlock, Message } from './shared';

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

  const { data, isLoading, isError, refetch } = useQuery<DaemonConfig>({
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
        <CardTitle>Daemon Control</CardTitle>
        <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted">
          <Loader2 size={16} className="animate-spin" />
          Loading daemon status…
        </div>
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card>
        <CardTitle>Daemon Control</CardTitle>
        <ErrorBlock label="Failed to load daemon status" onRetry={() => refetch()} />
      </Card>
    );
  }

  const isBusy = toggleBackgroundSync.isPending || restartDaemon.isPending;

  return (
    <Card>
      <CardTitle>Daemon Control</CardTitle>

      <div className="space-y-6">
        <div className="flex items-center justify-between">
          <span className="text-sm font-medium text-text">Background sync</span>
          <Toggle
            checked={data.enabled}
            disabled={isBusy}
            onChange={(enabled) => toggleBackgroundSync.mutate(enabled)}
            label="Background sync"
          />
        </div>

        {controlMessage && <Message tone={controlMessage.type}>{controlMessage.text}</Message>}

        <Btn onClick={() => restartDaemon.mutate()} disabled={isBusy} className="w-full justify-center">
          {restartDaemon.isPending ? (
            <>
              <Loader2 size={14} className="animate-spin" />
              Restarting…
            </>
          ) : (
            <>
              <RotateCw size={14} />
              Restart daemon
            </>
          )}
        </Btn>

        <div className="space-y-2">
          <label htmlFor="sync-scope" className="text-sm font-medium text-text">
            Scope
          </label>
          <select
            id="sync-scope"
            value={data.scope}
            onChange={(e) => updateConfig.mutate({ scope: e.target.value as 'personal' | 'team' })}
            className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-none"
          >
            <option value="personal">Personal</option>
            <option value="team">Team</option>
          </select>
          <p className="text-xs text-muted">
            {data.scope === 'personal'
              ? 'Sync all data across your own machines'
              : 'Share knowledge with your team (excludes transcripts)'}
          </p>
        </div>

        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <label htmlFor="sync-interval" className="text-sm font-medium text-text">
              Interval
            </label>
            <span className="text-sm text-muted">
              {Math.floor((intervalValue ?? data.interval) / 60)} minutes
            </span>
          </div>
          <input
            id="sync-interval"
            type="range"
            min={60}
            max={900}
            step={60}
            value={intervalValue ?? data.interval}
            onChange={(e) => setIntervalValue(Number(e.target.value))}
            // A native <input type="range"> fires React's onChange (mapped to
            // the DOM 'input' event) continuously while dragging - that's what
            // keeps the label live - but never fires anything on release by
            // itself. The Radix Slider this replaced had a separate
            // onValueCommit for that; here it's reconstructed from every way a
            // "release" can happen: pointer up, touch end, AND keyup (arrow-key
            // / Home / End users never fire mouseup/touchend at all).
            onMouseUp={() => {
              if (intervalValue != null) updateConfig.mutate({ interval: intervalValue });
            }}
            onTouchEnd={() => {
              if (intervalValue != null) updateConfig.mutate({ interval: intervalValue });
            }}
            onKeyUp={() => {
              if (intervalValue != null) updateConfig.mutate({ interval: intervalValue });
            }}
            className="w-full"
          />
          <p className="text-xs text-muted">How often to check for changes (1-15 minutes)</p>
        </div>
      </div>
    </Card>
  );
}
