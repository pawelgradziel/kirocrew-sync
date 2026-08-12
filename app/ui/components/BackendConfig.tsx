import React, { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardHeader, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { CheckCircle, XCircle, AlertTriangle, Loader2, ChevronDown, ChevronRight } from 'lucide-react';

type BackendName = 'gdrive' | 's3' | 'rsync' | 'local';

interface BackendInfo {
  name: BackendName;
  display_name: string;
  description: string;
  configured: boolean;
  active: boolean;
  requires_config: string[];
}

interface BackendsResponse {
  current: string;
  available: BackendInfo[];
}

interface BackendTestResult {
  success: boolean;
  message: string;
  details?: Record<string, unknown>;
}

interface BackendSwitchResult {
  success: boolean;
  backend: string;
}

interface BackendConfigField {
  key: string;
  value: string;
  source: 'config' | 'env' | 'default';
  is_set: boolean;
  required: boolean;
}

interface BackendConfigStatus {
  backend: BackendName;
  fields: BackendConfigField[];
}

async function fetchBackendConfig(backend: BackendName): Promise<BackendConfigStatus> {
  const response = await fetch(`/api/apps/kirocrew-sync/backends/${backend}/config`);
  if (!response.ok) throw new Error('Failed to fetch backend configuration');
  return response.json();
}

async function readErrorDetail(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    if (body && typeof body.detail === 'string') return body.detail;
  } catch {
    // response wasn't JSON - fall through to the generic message
  }
  return fallback;
}

function sourceLabel(field: BackendConfigField): string {
  if (field.source === 'config') return 'set in config.sh';
  if (field.source === 'env') return 'set via environment';
  return field.required ? 'required — not set' : 'using default';
}

/**
 * Per-backend settings form: fields are driven entirely by what
 * GET /backends/{name}/config returns (this app owns no hardcoded notion of
 * "the" fields), pre-filled with the effective value for each. Only fields
 * the user actually edits are included in the PUT body, so leaving a
 * defaulted field untouched never writes it to config.sh, and a redacted
 * value (see backend's _redact) is never silently re-submitted as the
 * literal string "***REDACTED***" unless the user retypes it themselves.
 */
function BackendConfigForm({ backend }: { backend: BackendName }) {
  const queryClient = useQueryClient();
  const [values, setValues] = useState<Record<string, string>>({});
  const [initialValues, setInitialValues] = useState<Record<string, string>>({});
  const [saveMessage, setSaveMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(
    null
  );

  const {
    data,
    isLoading,
    isError,
    refetch,
  } = useQuery<BackendConfigStatus>({
    queryKey: ['backend-config', backend],
    queryFn: () => fetchBackendConfig(backend),
  });

  useEffect(() => {
    if (!data) return;
    const next: Record<string, string> = {};
    data.fields.forEach((field) => {
      next[field.key] = field.value;
    });
    setValues(next);
    setInitialValues(next);
  }, [data]);

  const saveConfig = useMutation<BackendConfigStatus, Error, Record<string, string>>({
    mutationFn: async (config) => {
      const response = await fetch(`/api/apps/kirocrew-sync/backends/${backend}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config }),
      });
      if (!response.ok) {
        throw new Error(await readErrorDetail(response, 'Failed to save configuration'));
      }
      return response.json();
    },
    onSuccess: (result) => {
      setSaveMessage({ type: 'success', text: 'Configuration saved' });
      queryClient.setQueryData(['backend-config', backend], result);
      queryClient.invalidateQueries({ queryKey: ['backends'] });
    },
    onError: (error) => {
      setSaveMessage({ type: 'error', text: error.message });
    },
  });

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground py-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        Loading configuration...
      </div>
    );
  }

  if (isError || !data) {
    return (
      <div className="space-y-2 py-2">
        <p className="text-sm text-muted-foreground">Failed to load configuration</p>
        <Button size="sm" variant="outline" onClick={() => refetch()}>
          Retry
        </Button>
      </div>
    );
  }

  const dirtyEntries = Object.entries(values).filter(
    ([key, value]) => value !== (initialValues[key] ?? '')
  );
  const hasChanges = dirtyEntries.length > 0;

  const handleSave = () => {
    setSaveMessage(null);
    saveConfig.mutate(Object.fromEntries(dirtyEntries));
  };

  return (
    <div className="space-y-3 pt-3 mt-1 border-t">
      {data.fields.map((field) => (
        <div key={field.key} className="space-y-1">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <Label htmlFor={`${backend}-${field.key}`} className="text-xs font-medium">
              {field.key}
              {field.required && <span className="text-red-500 ml-0.5">*</span>}
            </Label>
            <span className="text-xs text-muted-foreground">{sourceLabel(field)}</span>
          </div>
          <Input
            id={`${backend}-${field.key}`}
            value={values[field.key] ?? ''}
            placeholder={field.required ? 'Required, no default' : undefined}
            disabled={saveConfig.isPending}
            onChange={(e) =>
              setValues((prev) => ({ ...prev, [field.key]: e.target.value }))
            }
            className="h-8 text-sm"
          />
        </div>
      ))}

      {data.fields.some((f) => f.required) && (
        <p className="text-xs text-muted-foreground">* required for this backend to work</p>
      )}

      {saveMessage && (
        <div
          className={
            saveMessage.type === 'success'
              ? 'p-2 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-md'
              : 'p-2 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md'
          }
        >
          <p
            className={
              saveMessage.type === 'success'
                ? 'text-sm text-green-800 dark:text-green-200 flex items-center gap-1'
                : 'text-sm text-red-800 dark:text-red-200 flex items-center gap-1'
            }
          >
            {saveMessage.type === 'success' ? (
              <CheckCircle className="w-4 h-4 shrink-0" />
            ) : (
              <XCircle className="w-4 h-4 shrink-0" />
            )}
            {saveMessage.text}
          </p>
        </div>
      )}

      <Button size="sm" onClick={handleSave} disabled={!hasChanges || saveConfig.isPending}>
        {saveConfig.isPending ? (
          <>
            <Loader2 className="w-4 h-4 mr-2 animate-spin" />
            Saving...
          </>
        ) : (
          'Save configuration'
        )}
      </Button>
    </div>
  );
}

export function BackendConfig() {
  const queryClient = useQueryClient();
  const [testResults, setTestResults] = useState<Record<string, BackendTestResult>>({});
  const [expanded, setExpanded] = useState<Set<BackendName>>(new Set());

  const {
    data,
    isLoading,
    isError,
    refetch,
  } = useQuery<BackendsResponse>({
    queryKey: ['backends'],
    queryFn: async () => {
      const response = await fetch('/api/apps/kirocrew-sync/backends');
      if (!response.ok) throw new Error('Failed to fetch backends');
      return response.json();
    },
  });

  const testBackend = useMutation<BackendTestResult, Error, BackendName>({
    mutationFn: async (backend) => {
      const response = await fetch('/api/apps/kirocrew-sync/backends/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend }),
      });
      if (!response.ok) throw new Error('Failed to test backend');
      return response.json();
    },
    onSuccess: (result, backend) => {
      setTestResults((prev) => ({ ...prev, [backend]: result }));
    },
    onError: (error, backend) => {
      setTestResults((prev) => ({
        ...prev,
        [backend]: { success: false, message: error.message },
      }));
    },
  });

  const switchBackend = useMutation<BackendSwitchResult, Error, BackendName>({
    mutationFn: async (backend) => {
      const response = await fetch('/api/apps/kirocrew-sync/backends/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend, config: {} }),
      });
      if (!response.ok) throw new Error('Failed to switch backend');
      return response.json();
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['backends'] });
      queryClient.invalidateQueries({ queryKey: ['sync-status'] });
    },
  });

  const toggleExpanded = (backend: BackendName) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(backend)) {
        next.delete(backend);
      } else {
        next.add(backend);
      }
      return next;
    });
  };

  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-8 text-center">
          <div className="flex items-center justify-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="w-4 h-4 animate-spin" />
            Loading backends...
          </div>
        </CardContent>
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <CardContent className="py-8 text-center space-y-3">
          <p className="text-sm text-muted-foreground">Failed to load backends</p>
          <Button size="sm" variant="outline" onClick={() => refetch()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  const backends = data?.available || [];

  if (backends.length === 0) {
    return (
      <Card>
        <CardContent className="py-8 text-center">
          <p className="text-sm text-muted-foreground">No backends available</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <h3 className="text-lg font-semibold">Storage Backend</h3>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {backends.map((backend) => {
          const isTesting = testBackend.isPending && testBackend.variables === backend.name;
          const isSwitching = switchBackend.isPending && switchBackend.variables === backend.name;
          const result = testResults[backend.name];
          const isExpanded = expanded.has(backend.name);

          return (
            <Card key={backend.name} className={backend.active ? 'border-primary' : undefined}>
              <CardHeader>
                <div className="flex items-start justify-between gap-2">
                  <div>
                    <h4 className="font-medium">{backend.display_name}</h4>
                    <p className="text-sm text-muted-foreground">{backend.description}</p>
                  </div>
                  {backend.active && (
                    <Badge className="bg-green-500 shrink-0">
                      <CheckCircle className="w-3 h-3 mr-1" />
                      Active
                    </Badge>
                  )}
                </div>
              </CardHeader>

              <CardContent className="space-y-3">
                <div className="flex items-center gap-2">
                  <Badge variant={backend.configured ? 'outline' : 'destructive'}>
                    {backend.configured ? 'Configured' : 'Not configured'}
                  </Badge>
                </div>

                {backend.requires_config.length > 0 && (
                  <div className="space-y-1">
                    <p className="text-xs text-muted-foreground">Requires</p>
                    <div className="flex flex-wrap gap-1">
                      {backend.requires_config.map((req) => (
                        <code
                          key={req}
                          className="text-xs bg-muted px-1 py-0.5 rounded"
                        >
                          {req}
                        </code>
                      ))}
                    </div>
                  </div>
                )}

                {result && (
                  <div
                    className={
                      result.success
                        ? 'p-2 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-md'
                        : 'p-2 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-md'
                    }
                  >
                    <p
                      className={
                        result.success
                          ? 'text-sm text-green-800 dark:text-green-200 flex items-center gap-1'
                          : 'text-sm text-red-800 dark:text-red-200 flex items-center gap-1'
                      }
                    >
                      {result.success ? (
                        <CheckCircle className="w-4 h-4 shrink-0" />
                      ) : (
                        <XCircle className="w-4 h-4 shrink-0" />
                      )}
                      {result.message}
                    </p>
                  </div>
                )}

                {!backend.configured && !result && (
                  <p className="text-xs text-muted-foreground flex items-center gap-1">
                    <AlertTriangle className="w-3 h-3 shrink-0" />
                    Set up this backend before switching to it
                  </p>
                )}

                <div className="flex gap-2 flex-wrap">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => testBackend.mutate(backend.name)}
                    disabled={isTesting}
                  >
                    {isTesting ? (
                      <>
                        <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                        Testing...
                      </>
                    ) : (
                      'Test Connection'
                    )}
                  </Button>
                  <Button
                    size="sm"
                    onClick={() => switchBackend.mutate(backend.name)}
                    disabled={backend.active || !backend.configured || isSwitching}
                  >
                    {isSwitching ? (
                      <>
                        <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                        Switching...
                      </>
                    ) : (
                      'Switch'
                    )}
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => toggleExpanded(backend.name)}
                    aria-expanded={isExpanded}
                    aria-label={isExpanded ? 'Hide configuration' : 'Configure'}
                  >
                    {isExpanded ? (
                      <ChevronDown className="w-4 h-4 mr-1" />
                    ) : (
                      <ChevronRight className="w-4 h-4 mr-1" />
                    )}
                    Configure
                  </Button>
                </div>

                {isExpanded && <BackendConfigForm backend={backend.name} />}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

export default BackendConfig;
