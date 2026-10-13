/**
 * Strike Desk V4.5 — Application Shell
 * TopBar (always visible) + collapsible NavSidebar + scrollable content.
 * systemStatus is fetched once here and passed down so all pages share the same poll.
 */
import React, { useState } from 'react';
import { Outlet } from 'react-router-dom';
import TopBar from './TopBar';
import NavSidebar from './NavSidebar';
import { useSystemStatus } from '../../hooks/useSystemStatus';

export default function AppShell() {
  const [collapsed, setCollapsed] = useState(false);
  const systemStatus = useSystemStatus();

  return (
    <div className="h-full flex flex-col bg-[#0B0F14]">
      {/* Persistent top safety bar */}
      <TopBar systemStatus={systemStatus} />

      {/* Body: sidebar + content */}
      <div className="flex flex-1 min-h-0">
        <NavSidebar
          systemStatus={systemStatus}
          collapsed={collapsed}
          onToggle={() => setCollapsed(v => !v)}
        />

        {/* Main content */}
        <main className="flex-1 min-w-0 overflow-y-auto bg-[#0B0F14]">
          <div className="max-w-[1600px] mx-auto p-5">
            <Outlet context={{ systemStatus }} />
          </div>
        </main>
      </div>
    </div>
  );
}
