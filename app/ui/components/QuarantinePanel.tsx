import React from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { ShieldAlert } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';

interface QuarantinedMachine {
  id: number;
  machine: string;
  reason: string;
  detected_at: string;
  details: string | null;
}

const DISMISS_HELP_TEXT =
  "Quarantine clears automatically once versions match on the next successful sync. Dismissing only hides this record locally — it does not force a merge.";

export function QuarantinePanel() {
  const queryClient = useQueryClient();
  const { data, isLoading, isError, refetch } = useQuery<{ machines: QuarantinedMachine[]; total: number }>({
    queryKey: ['quarantine'],
    queryFn: async () => {
      const response = await fetch('/api/apps/kirocrew-sync/quarantine');
      if (!response.ok) throw new Error('Failed to fetch quarantine');
      return response.json();
    },
  });

  const dismissQuarantine = useMutation({
    mutationFn: async (machine: string) => {
      const response = await fetch(
        `/api/apps/kirocrew-sync/quarantine/${encodeURIComponent(machine)}/clear`,
        { method: 'POST' }
      );
      if (!response.ok) throw new Error('Failed to dismiss quarantine record');
      return response.json();
    },
    onSuccess: () => {
      refetch();
      queryClient.invalidateQueries({ queryKey: ['sync-status'] });
    },
  });

  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-8 text-center">
          <p className="text-sm text-muted-foreground">Loading quarantined machines...</p>
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardContent className="py-8 text-center space-y-3">
          <p className="text-sm text-muted-foreground">Failed to load quarantined machines</p>
          <Button size="sm" variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const machines = data?.machines || [];

  if (machines.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center">
          <p className="text-sm text-muted-foreground">No machines in quarantine</p>
        </CardContent>
      </Card>
    );
  }

  const reasonLabels: Record<string, string> = {
    version_mismatch: 'Version Mismatch',
    embedding_mismatch: 'Embedding Mismatch',
    scope_mismatch: 'Scope Mismatch',
    other: 'Other',
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <h3 className="text-lg font-semibold">Quarantined Machines</h3>
          <Badge variant="destructive">{machines.length}</Badge>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        {machines.map((machine) => (
          <div
            key={machine.id}
            className="p-4 border border-orange-200 dark:border-orange-800 bg-orange-50 dark:bg-orange-900/20 rounded-lg"
          >
            <div className="flex items-start justify-between">
              <div className="flex-1">
                <div className="flex items-center gap-2 mb-2">
                  <ShieldAlert className="w-5 h-5 text-orange-500" />
                  <h4 className="font-medium">{machine.machine}</h4>
                  <Badge variant="outline">{reasonLabels[machine.reason] || machine.reason}</Badge>
                </div>
                {machine.details && (
                  <p className="text-sm text-muted-foreground mb-2">{machine.details}</p>
                )}
                <p className="text-xs text-muted-foreground mb-1">
                  Quarantined {formatDistanceToNow(new Date(machine.detected_at), { addSuffix: true })}
                </p>
                <p className="text-xs text-muted-foreground">{DISMISS_HELP_TEXT}</p>
              </div>
              <Button
                size="sm"
                variant="outline"
                title={DISMISS_HELP_TEXT}
                onClick={() => dismissQuarantine.mutate(machine.machine)}
                disabled={dismissQuarantine.isPending}
              >
                Dismiss
              </Button>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
