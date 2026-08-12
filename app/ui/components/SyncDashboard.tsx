import React from 'react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { StatusWidget } from './StatusWidget';
import { DaemonControl } from './DaemonControl';
import { HistoryTimeline } from './HistoryTimeline';
import { ConflictPanel } from './ConflictPanel';
import { QuarantinePanel } from './QuarantinePanel';
import { BackendConfig } from './BackendConfig';

export function SyncDashboard() {
  return (
    <div className="sync-dashboard p-6 space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <StatusWidget />
        <DaemonControl />
      </div>

      <Tabs defaultValue="history" className="w-full">
        <TabsList>
          <TabsTrigger value="history">History</TabsTrigger>
          <TabsTrigger value="conflicts">Conflicts</TabsTrigger>
          <TabsTrigger value="quarantine">Quarantine</TabsTrigger>
          <TabsTrigger value="backend">Backend</TabsTrigger>
        </TabsList>

        <TabsContent value="history" className="mt-6">
          <HistoryTimeline />
        </TabsContent>

        <TabsContent value="conflicts" className="mt-6">
          <ConflictPanel />
        </TabsContent>

        <TabsContent value="quarantine" className="mt-6">
          <QuarantinePanel />
        </TabsContent>

        <TabsContent value="backend" className="mt-6">
          <BackendConfig />
        </TabsContent>
      </Tabs>
    </div>
  );
}

export default SyncDashboard;
