import { useEffect, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardTitle, Btn, Badge, Input, EmptyState } from '@kirocrew/app-sdk/ui';
import lucideIcons from 'lucide-react';
import { CodePill, ErrorBlock, LoadingBlock, Message } from './shared';
import { apiFetchJson } from '../lib/api';

const { CheckCircle, AlertTriangle, Loader2, ChevronDown, ChevronRight, Server } = lucideIcons;

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
  return apiFetchJson(`/backends/${backend}/config`);
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

  const { data, isLoading, isError, error, refetch } = useQuery<BackendConfigStatus>({
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
    mutationFn: (config) =>
      apiFetchJson(`/backends/${backend}/config`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ config }),
      }),
    onSuccess: (result) => {
      setSaveMessage({ type: 'success', text: 'Configuration saved' });
      queryClient.setQueryData(['backend-config', backend], result);
      queryClient.invalidateQueries({ queryKey: ['backends'] });
    },
    onError: (error) => {
      setSaveMessage({ type: 'error', text: error.message });
    },
  });

  if (isLoading) return <LoadingBlock label="Loading configuration…" />;
  if (isError || !data) {
    return (
      <ErrorBlock label="Failed to load configuration" detail={error?.message} onRetry={() => refetch()} />
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
    <div className="space-y-3 pt-3 mt-3 border-t border-border">
      {data.fields.map((field) => (
        <div key={field.key} className="space-y-1">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <label htmlFor={`${backend}-${field.key}`} className="text-xs font-medium text-text">
              {field.key}
              {field.required && <span className="text-danger ml-0.5">*</span>}
            </label>
            <span className="text-xs text-muted">{sourceLabel(field)}</span>
          </div>
          <Input
            id={`${backend}-${field.key}`}
            value={values[field.key] ?? ''}
            placeholder={field.required ? 'Required, no default' : undefined}
            disabled={saveConfig.isPending}
            onChange={(e) => setValues((prev) => ({ ...prev, [field.key]: e.target.value }))}
            className="w-full"
          />
        </div>
      ))}

      {data.fields.some((f) => f.required) && (
        <p className="text-xs text-muted">* required for this backend to work</p>
      )}

      {saveMessage && <Message tone={saveMessage.type}>{saveMessage.text}</Message>}

      <Btn primary onClick={handleSave} disabled={!hasChanges || saveConfig.isPending}>
        {saveConfig.isPending ? (
          <>
            <Loader2 size={14} className="animate-spin" />
            Saving…
          </>
        ) : (
          'Save configuration'
        )}
      </Btn>
    </div>
  );
}

export function BackendConfig() {
  const queryClient = useQueryClient();
  const [testResults, setTestResults] = useState<Record<string, BackendTestResult>>({});
  const [switchErrors, setSwitchErrors] = useState<Record<string, string>>({});
  const [expanded, setExpanded] = useState<Set<BackendName>>(new Set());

  const { data, isLoading, isError, error, refetch } = useQuery<BackendsResponse>({
    queryKey: ['backends'],
    queryFn: () => apiFetchJson('/backends'),
  });

  const testBackend = useMutation<BackendTestResult, Error, BackendName>({
    mutationFn: (backend) =>
      apiFetchJson('/backends/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend }),
      }),
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
    mutationFn: (backend) =>
      apiFetchJson('/backends/switch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend, config: {} }),
      }),
    onSuccess: (_result, backend) => {
      setSwitchErrors((prev) => {
        const { [backend]: _dropped, ...rest } = prev;
        return rest;
      });
      queryClient.invalidateQueries({ queryKey: ['backends'] });
      queryClient.invalidateQueries({ queryKey: ['sync-status'] });
    },
    onError: (err, backend) => {
      setSwitchErrors((prev) => ({ ...prev, [backend]: err.message }));
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
        <LoadingBlock label="Loading backends…" />
      </Card>
    );
  }

  if (isError) {
    return (
      <Card>
        <ErrorBlock label="Failed to load backends" detail={error?.message} onRetry={() => refetch()} />
      </Card>
    );
  }

  const backends = data?.available || [];

  if (backends.length === 0) {
    return (
      <Card>
        <EmptyState icon={<Server size={40} />} title="No backends available" />
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-semibold tracking-tight text-text-strong">Storage Backend</h3>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        {backends.map((backend) => {
          const isTesting = testBackend.isPending && testBackend.variables === backend.name;
          const isSwitching = switchBackend.isPending && switchBackend.variables === backend.name;
          const result = testResults[backend.name];
          const switchError = switchErrors[backend.name];
          const isExpanded = expanded.has(backend.name);

          return (
            <Card key={backend.name} className={backend.active ? 'border-accent' : undefined}>
              <div className="flex items-start justify-between gap-2 mb-3">
                <div className="min-w-0">
                  <h4 className="font-medium text-text">{backend.display_name}</h4>
                  <p className="text-sm text-muted">{backend.description}</p>
                </div>
                {backend.active && (
                  <Badge variant="ok">
                    <CheckCircle size={12} />
                    Active
                  </Badge>
                )}
              </div>

              <div className="space-y-3">
                <Badge variant={backend.configured ? 'muted' : 'err'}>
                  {backend.configured ? 'Configured' : 'Not configured'}
                </Badge>

                {backend.requires_config.length > 0 && (
                  <div className="space-y-1">
                    <p className="text-xs text-muted">Requires</p>
                    <div className="flex flex-wrap gap-1">
                      {backend.requires_config.map((req) => (
                        <CodePill key={req}>{req}</CodePill>
                      ))}
                    </div>
                  </div>
                )}

                {result && <Message tone={result.success ? 'success' : 'error'}>{result.message}</Message>}

                {switchError && <Message tone="error">{switchError}</Message>}

                {!backend.configured && !result && (
                  <p className="text-xs text-muted flex items-center gap-1">
                    <AlertTriangle size={12} className="shrink-0" />
                    Set up this backend before switching to it
                  </p>
                )}

                <div className="flex gap-2 flex-wrap">
                  <Btn onClick={() => testBackend.mutate(backend.name)} disabled={isTesting}>
                    {isTesting ? (
                      <>
                        <Loader2 size={14} className="animate-spin" />
                        Testing…
                      </>
                    ) : (
                      'Test Connection'
                    )}
                  </Btn>
                  <Btn
                    primary
                    onClick={() => switchBackend.mutate(backend.name)}
                    disabled={backend.active || !backend.configured || isSwitching}
                  >
                    {isSwitching ? (
                      <>
                        <Loader2 size={14} className="animate-spin" />
                        Switching…
                      </>
                    ) : (
                      'Switch'
                    )}
                  </Btn>
                  <Btn
                    onClick={() => toggleExpanded(backend.name)}
                    aria-expanded={isExpanded}
                    aria-label={isExpanded ? 'Hide configuration' : 'Configure'}
                  >
                    {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                    Configure
                  </Btn>
                </div>

                {isExpanded && <BackendConfigForm backend={backend.name} />}
              </div>
            </Card>
          );
        })}
      </div>
    </div>
  );
}

export default BackendConfig;
