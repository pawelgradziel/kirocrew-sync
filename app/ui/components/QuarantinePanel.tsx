import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardTitle, Btn, Badge, EmptyState } from '@kirocrew/app-sdk/ui';
import lucideIcons from 'lucide-react';
import { ErrorBlock, LoadingBlock, Message } from './shared';
import { formatRelativeTime } from '../lib/time';
import { apiFetchJson } from '../lib/api';

const { ShieldAlert, ShieldCheck } = lucideIcons;

interface QuarantinedMachine {
  id: number;
  machine: string;
  reason: string;
  detected_at: string;
  details: string | null;
}

const DISMISS_HELP_TEXT =
  "Quarantine clears automatically once versions match on the next successful sync. Dismissing only hides this record locally — it does not force a merge.";

const REASON_LABELS: Record<string, string> = {
  version_mismatch: 'Version Mismatch',
  embedding_mismatch: 'Embedding Mismatch',
  scope_mismatch: 'Scope Mismatch',
  other: 'Other',
};

export function QuarantinePanel() {
  const queryClient = useQueryClient();
  const [dismissError, setDismissError] = useState<string | null>(null);

  const { data, isLoading, isError, error, refetch } = useQuery<{ machines: QuarantinedMachine[]; total: number }>({
    queryKey: ['quarantine'],
    queryFn: () => apiFetchJson('/quarantine'),
  });

  const dismissQuarantine = useMutation({
    mutationFn: (machine: string) =>
      apiFetchJson(`/quarantine/${encodeURIComponent(machine)}/clear`, { method: 'POST' }),
    onSuccess: () => {
      setDismissError(null);
      refetch();
      queryClient.invalidateQueries({ queryKey: ['sync-status'] });
    },
    onError: (err: Error) => setDismissError(err.message),
  });

  if (isLoading) {
    return (
      <Card>
        <LoadingBlock label="Loading quarantined machines…" />
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <ErrorBlock
          label="Failed to load quarantined machines"
          detail={error?.message}
          onRetry={() => refetch()}
        />
      </Card>
    );
  }

  const machines = data?.machines || [];

  if (machines.length === 0) {
    return (
      <Card>
        <EmptyState icon={<ShieldCheck size={40} />} title="No machines in quarantine" />
      </Card>
    );
  }

  return (
    <Card>
      <div className="flex items-center justify-between mb-4">
        <CardTitle className="mb-0">Quarantined Machines</CardTitle>
        <Badge variant="err">{machines.length}</Badge>
      </div>

      {dismissError && (
        <div className="mb-4">
          <Message tone="error">{dismissError}</Message>
        </div>
      )}

      <div className="space-y-4">
        {machines.map((machine) => (
          <div key={machine.id} className="p-4 border border-warn/30 bg-warn-subtle rounded-lg">
            <div className="flex items-start justify-between gap-3">
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2 mb-2 flex-wrap">
                  <ShieldAlert size={18} className="text-warn shrink-0" />
                  <h4 className="font-medium text-text">{machine.machine}</h4>
                  <Badge variant="muted">{REASON_LABELS[machine.reason] || machine.reason}</Badge>
                </div>
                {machine.details && <p className="text-sm text-muted mb-2">{machine.details}</p>}
                <p className="text-xs text-muted mb-1">
                  Quarantined {formatRelativeTime(machine.detected_at)}
                </p>
                <p className="text-xs text-muted">{DISMISS_HELP_TEXT}</p>
              </div>
              <Btn
                title={DISMISS_HELP_TEXT}
                onClick={() => dismissQuarantine.mutate(machine.machine)}
                disabled={dismissQuarantine.isPending}
              >
                Dismiss
              </Btn>
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}
