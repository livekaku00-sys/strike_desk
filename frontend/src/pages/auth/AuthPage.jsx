/**
 * Strike Desk V4.5 — Auth / Session Page
 * Shows current session, role, and token status. Read-only.
 */
import React, { useState, useCallback, useEffect } from 'react';
import { auth as authApi } from '../../api/services';
import { useAuthStore } from '../../store/authStore';
import {
  Panel, PanelHeader, StatusBadge, KV, LoadingBlock, PageHeader, ActionBtn, ErrorBlock, BackendRequired, Freshness,
} from '../../components/ui';

const POLL = 60_000; // re-check token expiry every 60s

function ConfirmDialog({ message, onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70">
      <div className="bg-[#111821] border border-[#2A3F5A] rounded-lg p-6 max-w-sm w-full mx-4">
        <div className="text-[14px] font-bold text-[#F5F7FA] mb-2">Confirm Logout</div>
        <div className="text-[12px] text-[#8D98A8] mb-5">{message}</div>
        <div className="flex gap-3 justify-end">
          <ActionBtn onClick={onCancel}>Cancel</ActionBtn>
          <ActionBtn color="amber" onClick={onConfirm}>Log Out</ActionBtn>
        </div>
      </div>
    </div>
  );
}

function maskToken(token) {
  if (!token) return '—';
  if (token.length <= 12) return '••••••••';   // too short to safely slice
  return `${token.substring(0, 6)}…${token.substring(token.length - 6)}`;
}

export default function AuthPage() {
  const { user, role, token, logout, selectedExecutionBroker } = useAuthStore();

  const [tokenStatus, setTokenStatus] = useState(null);
  const [meData,      setMeData]      = useState(null);
  // Separate initial-load from background-refresh so stale data stays visible while refreshing
  const [initialLoad,    setInitialLoad]    = useState(true);
  const [refreshing,     setRefreshing]     = useState(false);
  const [meErr,          setMeErr]          = useState(null);
  const [tsErr,          setTsErr]          = useState(null);
  const [showLogout,     setShowLogout]     = useState(false);
  const [lastCheckedAt,  setLastCheckedAt]  = useState(null);

  const fetch = useCallback(async (isManual = false) => {
    if (isManual) setRefreshing(true);

    // allSettled never throws — handle each promise result individually
    const [rMe, rTs] = await Promise.allSettled([
      authApi.me(),
      authApi.tokenStatus(),
    ]);

    if (rMe.status === 'fulfilled') {
      const d = rMe.value.data;
      // Guard: only use if it's a plain non-null object
      setMeData(d && typeof d === 'object' && !Array.isArray(d) ? d : null);
      setMeErr(null);
    } else {
      setMeErr(rMe.reason?.response?.data?.detail || rMe.reason?.message || 'Failed to fetch /me');
    }

    if (rTs.status === 'fulfilled') {
      setTokenStatus(rTs.value.data ?? null);
      setTsErr(null);
    } else {
      // 404 means endpoint not implemented yet; surface as BackendRequired not error
      const status = rTs.reason?.response?.status;
      if (status === 404 || status === 501) {
        setTokenStatus(null);
        setTsErr(null);
      } else {
        setTsErr(rTs.reason?.response?.data?.detail || rTs.reason?.message || 'Failed to check token');
      }
    }

    setLastCheckedAt(Date.now());
    setInitialLoad(false);
    if (isManual) setRefreshing(false);
  }, []);

  useEffect(() => {
    fetch();
    const t = setInterval(() => fetch(), POLL);
    return () => clearInterval(t);
  }, [fetch]);

  const roleColor = role === 'operator' ? 'green' : role === 'guest' ? 'amber' : 'gray';
  const maskedToken = maskToken(token);

  const meEntries = meData ? Object.entries(meData) : [];

  return (
    <div>
      {showLogout && (
        <ConfirmDialog
          message="You will be signed out and redirected to the login page."
          onConfirm={() => { logout(); setShowLogout(false); }}
          onCancel={() => setShowLogout(false)}
        />
      )}

      <PageHeader
        title="Session & Auth"
        subtitle="Current session, role, and token status."
        right={
          <ActionBtn onClick={() => fetch(true)} disabled={refreshing}>
            {refreshing ? 'Refreshing…' : 'Refresh'}
          </ActionBtn>
        }
      />

      {/* Current Session */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4">
        <Panel>
          <PanelHeader title="CURRENT SESSION" subtitle="From local auth store" />
          <div className="p-4 space-y-1">
            <KV label="User"
                value={user?.username ?? meData?.username ?? '—'} />
            <KV label="Role"
                value={<StatusBadge text={role?.toUpperCase() ?? 'UNKNOWN'} color={roleColor} />} />
            <KV label="Exec. Broker"
                value={<span className="font-mono text-[#8D98A8]">{selectedExecutionBroker?.toUpperCase() ?? '—'}</span>} />
            <KV label="Token (masked)"
                value={<span className="font-mono text-[11px] text-[#4A5568] break-all">{maskedToken}</span>} />
            <KV label="Session ID"
                value={<span className="font-mono text-[10px] text-[#4A5568]">{user?.id ?? '—'}</span>} />
            <KV label="Authenticated"
                value={<StatusBadge text={token ? 'YES' : 'NO'} color={token ? 'green' : 'red'} />} />
            <KV label="Last checked"
                value={<Freshness ageMs={lastCheckedAt} warnMs={90_000} errorMs={180_000} />}
                mono={false} />
          </div>
        </Panel>

        <Panel>
          <PanelHeader title="TOKEN STATUS" subtitle="Live from backend" />
          {initialLoad ? (
            <LoadingBlock label="Checking token…" />
          ) : tsErr ? (
            <ErrorBlock label="TOKEN CHECK FAILED" detail={tsErr} />
          ) : !tokenStatus ? (
            <BackendRequired endpoint="GET /api/v1/auth/token-status" />
          ) : (
            <div className="p-4 space-y-1">
              <KV label="Valid"
                  value={<StatusBadge
                    text={tokenStatus.valid ? 'VALID' : 'INVALID'}
                    color={tokenStatus.valid ? 'green' : 'red'}
                  />} />
              <KV label="Expires"
                  value={tokenStatus.expires_at ?? (tokenStatus.exp ? new Date(tokenStatus.exp * 1000).toISOString() : '—')} />
              <KV label="Issued"
                  value={tokenStatus.issued_at  ?? (tokenStatus.iat ? new Date(tokenStatus.iat * 1000).toISOString() : '—')} />
              <KV label="Token type"  value={tokenStatus.token_type ?? 'bearer'} />
              {tokenStatus.username && <KV label="Subject" value={tokenStatus.username} />}
              {tokenStatus.role     && <KV label="Role"    value={tokenStatus.role} />}
            </div>
          )}
          {refreshing && !initialLoad && (
            <div className="px-4 pb-2 text-[10px] font-mono text-[#4A5568] animate-pulse">re-checking…</div>
          )}
        </Panel>
      </div>

      {/* Backend /me data */}
      <Panel>
        <PanelHeader title="BACKEND IDENTITY" subtitle="From /api/v1/auth/me" />
        {initialLoad ? (
          <LoadingBlock />
        ) : meErr ? (
          <ErrorBlock label="IDENTITY FETCH FAILED" detail={meErr} />
        ) : meEntries.length === 0 ? (
          <BackendRequired endpoint="GET /api/v1/auth/me" />
        ) : (
          <div className="p-4">
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-x-8">
              {meEntries.map(([k, v]) => (
                <KV key={k} label={k} value={
                  v === null || v === undefined
                    ? <span className="text-[#4A5568]">null</span>
                    : typeof v === 'boolean'
                    ? <StatusBadge text={v ? 'YES' : 'NO'} color={v ? 'green' : 'gray'} />
                    : typeof v === 'object'
                    ? <span className="font-mono text-[10px] text-[#4A5568] break-all">{JSON.stringify(v)}</span>
                    : String(v)
                } />
              ))}
            </div>
          </div>
        )}
      </Panel>

      {/* Role capabilities */}
      <Panel className="mt-4">
        <PanelHeader title="ROLE CAPABILITIES" />
        <div className="divide-y divide-[#1E2A3A]">
          {[
            ['View all pages & data',    true,  true],
            ['Place orders',             true,  false],
            ['Halt / resume trading',    true,  false],
            ['Arm LIVE mode',            true,  false],
            ['Re-auth brokers',          true,  false],
            ['Change execution mode',    true,  false],
            ['Square off positions',     true,  false],
            ['Reset risk limits',        true,  false],
          ].map(([cap, op, gu]) => {
            // Highlight rows where current role is denied
            const currentDenied =
              (role === 'operator' && !op) || (role === 'guest' && !gu);
            return (
              <div key={cap} className={`flex items-center gap-3 px-4 py-2.5 ${currentDenied ? 'bg-red-950/5' : ''}`}>
                <span className="flex-1 text-[12px] text-[#8D98A8]">{cap}</span>
                <div className="flex items-center gap-1.5">
                  <span className="text-[9px] font-mono text-[#4A5568] uppercase">op</span>
                  <StatusBadge text={op ? 'Y' : 'N'} color={op ? 'green' : 'gray'} />
                </div>
                <div className="flex items-center gap-1.5 ml-3">
                  <span className="text-[9px] font-mono text-[#4A5568] uppercase">guest</span>
                  <StatusBadge text={gu ? 'Y' : 'N'} color={gu ? 'green' : 'gray'} />
                </div>
                {role && (
                  <div className="ml-3 w-16 text-right">
                    {(role === 'operator' ? op : gu)
                      ? <span className="text-[10px] font-mono text-green-400">✓ yours</span>
                      : <span className="text-[10px] font-mono text-red-400/60">✗ yours</span>}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </Panel>

      {/* Logout — operator only, uses inline dialog not window.confirm */}
      {role === 'operator' && (
        <div className="mt-4 flex items-center gap-3">
          <ActionBtn color="amber" onClick={() => setShowLogout(true)}>
            Sign Out
          </ActionBtn>
          <span className="text-[10px] font-mono text-[#4A5568]">
            Clears local session. Backend token is not revoked unless /api/v1/auth/logout is implemented.
          </span>
        </div>
      )}
    </div>
  );
}
