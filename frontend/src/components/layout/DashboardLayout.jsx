import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Outlet } from 'react-router-dom';
import Sidebar from './Sidebar';
import Header from './Header';
import Footer from './Footer';
import AuditTrail from '../AuditTrail';
import { useCanTrade } from '../../store/authStore';
import ToastContainer, { triggerToast } from '../ToastContainer';
import useWebSocket from '../../hooks/useWebSocket';
import { useAuthStore } from '../../store/authStore';
import apiClient from '../../api/client';
import { SystemStateContext } from '../../context/SystemStateContext';
import { REQ, fetchJson, statusFromFetchFailure } from '../../utils/requestState';

export const SYSTEM_POLL_MS = 5000;

export default function DashboardLayout() {
  const { selectedAssetClass, setAssetClass } = useAuthStore();

  // Seeded from the last selection so the sidebar has something to highlight on
  // first paint; classLock.status stays LOADING (shown as UNCONFIRMED) until the
  // server's value arrives.
  const [activeMode, setActiveMode] = useState(selectedAssetClass || 'AUTO_OPTIONS');
  const [classLock, setClassLock] = useState({ status: REQ.LOADING, updatedAt: null });
  const [execMode, setExecMode] = useState({ status: REQ.LOADING, value: null, updatedAt: null });
  // No placeholder numbers: until /risk/state answers there is no risk state.
  const [risk, setRisk] = useState({ status: REQ.LOADING, value: null, updatedAt: null });
  // Timestamp of the last operator-initiated mode change. The background poll
  // must not overwrite the user's selection for a brief window after they act,
  // otherwise an in-flight POST races with the next GET and the UI reverts.
  const _lastModeChangedAt = useRef(0);
  const [showAuditTrail, setShowAuditTrail] = useState(false);
  const canTrade = useCanTrade();
  const { isConnected } = useWebSocket();

  // Persists the operator's class selection to the server.
  //
  // Optimistic update: the UI switches immediately so the change feels instant.
  // If the server rejects the POST, the previous mode is restored. The ref
  // timestamp tells the background poll not to overwrite this selection for
  // 10 seconds, covering the POST round-trip and any in-flight GET.
  const handleModeChange = useCallback(async (mode) => {
    const previous = activeMode;
    setActiveMode(mode);
    setAssetClass(mode);
    _lastModeChangedAt.current = Date.now();
    try {
      await apiClient.post('/api/v1/trading/mode', { mode });
    } catch (e) {
      // Server rejected the change — revert so the UI stays truthful.
      setActiveMode(previous);
      setAssetClass(previous);
      _lastModeChangedAt.current = 0;
      console.warn('[DashboardLayout] Mode change rejected:', e.response?.data?.detail || e.message);
    }
  }, [activeMode, setAssetClass]);

  const fetchSystemState = useCallback(async () => {
    // Raw fetch has no built-in timeout. AbortController ensures each poll call
    // gives up after 5 seconds so a slow/unreachable server cannot freeze the
    // dashboard UI between refresh intervals; an abort is reported as TIMEOUT.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    const opts = { signal: controller.signal };
    try {
      const [rMode, rRisk, rExec] = await Promise.allSettled([
        fetchJson('/api/v1/trading/mode', opts),
        fetchJson('/api/v1/risk/state', opts),
        fetchJson('/api/v1/trading/execution-mode', opts),
      ]);
      const now = Date.now();

      if (rMode.status === 'fulfilled' && rMode.value?.active_mode) {
        // Only sync from the server if the operator hasn't touched the selector
        // in the last 10 seconds. Within that window their optimistic update is
        // authoritative; outside it the server is the source of truth (e.g. after
        // a page refresh or a change made from another tab).
        const msSinceLastChange = now - _lastModeChangedAt.current;
        if (msSinceLastChange > 10_000) {
          setActiveMode(rMode.value.active_mode);
        }
        setClassLock({ status: REQ.OK, updatedAt: now });
      } else {
        const status = rMode.status === 'fulfilled' ? REQ.API_ERROR : statusFromFetchFailure(rMode.reason);
        setClassLock((prev) => ({ status, updatedAt: prev.updatedAt }));
      }

      if (rRisk.status === 'fulfilled' && rRisk.value && typeof rRisk.value === 'object') {
        setRisk({ status: REQ.OK, value: rRisk.value, updatedAt: now });
        if (rRisk.value.emergency_halt_active) {
          triggerToast({
            type: 'HALT',
            title: 'EMERGENCY HALT ACTIVE',
            message: 'Order execution is globally locked.',
          });
        }
      } else {
        // Keep the last good value so the footer can show it, marked STALE.
        const status = rRisk.status === 'fulfilled' ? REQ.API_ERROR : statusFromFetchFailure(rRisk.reason);
        setRisk((prev) => ({ ...prev, status }));
      }

      if (rExec.status === 'fulfilled' && rExec.value?.execution_mode) {
        setExecMode({ status: REQ.OK, value: rExec.value.execution_mode, updatedAt: now });
      } else {
        // Never carried over: a stale PAPER/LIVE is worse than an explicit error.
        const status = rExec.status === 'fulfilled' ? REQ.API_ERROR : statusFromFetchFailure(rExec.reason);
        setExecMode({ status, value: null, updatedAt: null });
      }
    } finally {
      clearTimeout(timer);
    }
  }, []);

  useEffect(() => {
    fetchSystemState();
    const interval = setInterval(fetchSystemState, SYSTEM_POLL_MS);
    return () => clearInterval(interval);
  }, [fetchSystemState]);

  useEffect(() => {
    const handleKey = (e) => {
      if (e.ctrlKey && (e.key === 'h' || e.key === 'H')) {
        e.preventDefault();
        triggerToast({
          type: 'HALT',
          title: 'KEYBOARD SHORTCUT',
          message: 'Ctrl+H: Click Emergency Halt in the footer to confirm.',
        });
      }
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, []);

  const systemState = useMemo(() => ({ execMode, classLock, risk }), [execMode, classLock, risk]);
  const outletCtx = { activeMode, classLock, riskState: risk.value, isConnected };

  return (
    <SystemStateContext.Provider value={systemState}>
      <div className="flex h-screen bg-slate-900 text-slate-100 overflow-hidden">
        <ToastContainer />
        {canTrade && (
          <AuditTrail isOpen={showAuditTrail} onClose={() => setShowAuditTrail(false)} />
        )}

        {/* SIDEBAR: Fixed 256px */}
        <Sidebar
          activeMode={activeMode}
          onModeChange={handleModeChange}
        />

        {/* MAIN AREA */}
        <div className="flex-1 flex flex-col min-w-0">
          {/* HEADER: Fixed 64px */}
          <Header
            isConnected={isConnected}
            onAuditTrail={canTrade ? () => setShowAuditTrail(true) : undefined}
          />

          {/* BODY: scrolls */}
          <main className="flex-1 overflow-y-auto p-6 space-y-6">
            <Outlet context={outletCtx} />
          </main>

          {/* FOOTER: Fixed 56px */}
          <Footer />
        </div>
      </div>
    </SystemStateContext.Provider>
  );
}
