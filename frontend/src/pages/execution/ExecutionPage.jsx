/**
 * Strike Desk V4.5 — Execution Page
 * PAPER ↔ LIVE arming with multi-step safety gates.
 * Never optimistically shows LIVE. Backend confirmation required.
 */
import React, { useState, useCallback, useEffect, useRef } from 'react';
import {
  execution as execApi, brokers as brokerApi,
  risk as riskApi, strategy as strategyApi, market as marketApi,
  preflight as preflightApi,
} from '../../api/services';
import { useAuthStore } from '../../store/authStore';
import {
  Panel, PanelHeader, StatusBadge, KV, LoadingBlock, ErrorBlock,
  EmptyBlock, PageHeader, ActionBtn, Freshness,
} from '../../components/ui';

const POLL          = 10_000;
const STALE_WARN_MS = 25_000;
const STALE_ERR_MS  = 45_000;

function GateRow({ label, status, detail }) {
  const icon  = status === 'PASS' ? '✓' : status === 'FAIL' ? '✗' : status === 'CHECKING' ? '…' : '—';
  const color = status === 'PASS' ? 'text-green-300' : status === 'FAIL' ? 'text-red-400' : 'text-[#8D98A8]';
  return (
    <div className="flex items-center gap-3 py-2 border-b border-[#1E2A3A]">
      <span className={`font-mono font-bold text-[13px] w-4 ${color}`}>{icon}</span>
      <span className="text-[12px] text-[#F5F7FA] flex-1">{label}</span>
      {status === 'FAIL' && detail && (
        <span className="text-[11px] text-red-400/70 font-mono">{detail}</span>
      )}
      {status === 'PASS' && <StatusBadge text="PASS" color="green" />}
      {status === 'FAIL' && <StatusBadge text="FAIL" color="red" />}
    </div>
  );
}

function LiveArmingModal({ checks, onArm, onCancel, arming }) {
  const allPass = Object.values(checks).every(c => c.status === 'PASS');
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/80">
      <div className="bg-[#111821] border border-[#2A3F5A] rounded-lg p-6 max-w-lg w-full mx-4">
        <div className="text-[15px] font-bold text-[#F5F7FA] mb-1">ARM LIVE EXECUTION</div>
        <div className="text-[11px] text-[#8D98A8] mb-4">
          All gates must pass before arming. Orders will be sent to the live broker.
        </div>
        <div className="space-y-0 mb-4">
          {Object.entries(checks).map(([key, c]) => (
            <GateRow key={key} label={c.label} status={c.status} detail={c.detail} />
          ))}
        </div>
        {allPass && (
          <div className="mb-4 p-3 bg-red-950/30 border border-red-700/40 rounded text-[12px] font-mono text-red-300">
            ⚠ WARNING: ORDERS MAY BE SENT TO THE LIVE BROKER. REAL CAPITAL AT RISK.
          </div>
        )}
        <div className="flex gap-3 justify-end">
          <ActionBtn onClick={onCancel} disabled={arming}>Cancel</ActionBtn>
          <ActionBtn color="danger" onClick={onArm} disabled={!allPass || arming}>
            {arming ? 'Arming…' : 'ARM LIVE EXECUTION'}
          </ActionBtn>
        </div>
      </div>
    </div>
  );
}

export default function ExecutionPage() {
  const { role } = useAuthStore();
  const isOperator = role === 'operator';

  const [initialLoad,  setInitialLoad]  = useState(true);
  const [refreshing,   setRefreshing]   = useState(false);
  const reqId = useRef(0);

  const [execData,     setExecData]     = useState(null);
  const [brokerData,   setBrokerData]   = useState(null);
  const [riskData,     setRiskData]     = useState(null);
  const [signals,      setSignals]      = useState([]);
  const [positions,    setPositions]    = useState([]);
  const [paperAccount, setPaperAccount] = useState(null);
  const [err,          setErr]          = useState(null);
  const [ageMs,        setAgeMs]        = useState(null);

  const [showArm, setShowArm] = useState(false);
  const [arming,  setArming]  = useState(false);
  const [armMsg,  setArmMsg]  = useState(null);
  const [checks,  setChecks]  = useState({});

  const fetchAll = useCallback(async () => {
    const id = ++reqId.current;
    setRefreshing(true);

    const [rExec, rBrokers, rRisk, rSignals, rPositions, rPaper] = await Promise.allSettled([
      execApi.mode(),
      brokerApi.tokensStatus(),
      riskApi.state(),
      strategyApi.signals(),
      execApi.positions(),
      execApi.paperAccount(),
    ]);

    if (id !== reqId.current) return;

    if (rExec.status === 'fulfilled') {
      setExecData(rExec.value.data);
      setErr(null);
    } else {
      setErr(rExec.reason?.response?.data?.detail || rExec.reason?.message || 'Execution mode unavailable');
    }

    if (rBrokers.status === 'fulfilled') setBrokerData(rBrokers.value.data);

    if (rRisk.status === 'fulfilled') setRiskData(rRisk.value.data);

    if (rSignals.status === 'fulfilled') {
      const raw = Array.isArray(rSignals.value.data)
        ? rSignals.value.data
        : (rSignals.value.data?.signals ?? []);
      setSignals(raw.filter(s => s.status === 'APPROVED_FOR_EXECUTION'));
    }

    if (rPositions.status === 'fulfilled') {
      const raw = Array.isArray(rPositions.value.data)
        ? rPositions.value.data
        : (rPositions.value.data?.positions ?? []);
      setPositions(raw);
    }

    if (rPaper.status === 'fulfilled') setPaperAccount(rPaper.value.data);

    setAgeMs(Date.now());
    setInitialLoad(false);
    setRefreshing(false);
  }, []);

  useEffect(() => {
    fetchAll();
    const t = setInterval(fetchAll, POLL);
    return () => clearInterval(t);
  }, [fetchAll]);

  const runChecks = async () => {
    const initial = {
      broker:   { label: 'Broker Connected',    status: 'CHECKING', detail: '' },
      token:    { label: 'Token Valid',          status: 'CHECKING', detail: '' },
      market:   { label: 'Market Hours',         status: 'CHECKING', detail: '' },
      risk:     { label: 'Risk Clear',           status: 'CHECKING', detail: '' },
      strategy: { label: 'Strategy Active',      status: 'CHECKING', detail: '' },
      data:     { label: 'Market Data Pipeline', status: 'CHECKING', detail: '' },
      position: { label: 'No Open Position',     status: 'CHECKING', detail: '' },
    };
    setChecks(initial);

    const [rBrokers, rRisk, rStrat, rPipeline, rPos, rPreflight] = await Promise.allSettled([
      brokerApi.tokensStatus(),
      riskApi.state(),
      strategyApi.mode(),
      marketApi.pipelineStatus(),
      execApi.positions(),
      preflightApi.run(),
    ]);

    const updated = { ...initial };

    if (rBrokers.status === 'fulfilled') {
      const list = rBrokers.value.data?.brokers ?? [];
      const dhan = list.find(b => b.broker_name === 'dhan');
      updated.broker = { ...updated.broker,
        status: dhan?.is_authenticated ? 'PASS' : 'FAIL',
        detail: dhan?.is_authenticated ? '' : 'Dhan not authenticated',
      };
      updated.token = { ...updated.token,
        status: dhan?.status === 'ACTIVE' ? 'PASS' : 'FAIL',
        detail: dhan?.status ?? 'Unknown',
      };
    } else {
      updated.broker = { ...updated.broker, status: 'FAIL', detail: 'Cannot reach broker API' };
      updated.token  = { ...updated.token,  status: 'FAIL', detail: 'Cannot reach broker API' };
    }

    if (rRisk.status === 'fulfilled') {
      const rd = rRisk.value.data;
      const halted = rd?.emergency_halt_active || rd?.loss_ceiling_breached || rd?.cooldown_active;
      updated.risk = { ...updated.risk,
        status: halted ? 'FAIL' : 'PASS',
        detail: rd?.emergency_halt_active ? 'Halt active'
              : rd?.loss_ceiling_breached  ? 'Loss ceiling breached'
              : rd?.cooldown_active        ? 'Cooldown active'
              : '',
      };
    } else {
      updated.risk = { ...updated.risk, status: 'FAIL', detail: 'Cannot reach risk API' };
    }

    if (rStrat.status === 'fulfilled') {
      const m = rStrat.value.data?.mode ?? rStrat.value.data?.active_mode;
      updated.strategy = { ...updated.strategy,
        status: m ? 'PASS' : 'FAIL',
        detail: m ? `Mode: ${m}` : 'No active strategy mode',
      };
    } else {
      updated.strategy = { ...updated.strategy, status: 'FAIL', detail: 'Cannot reach strategy API' };
    }

    if (rPipeline.status === 'fulfilled') {
      const p = rPipeline.value.data;
      const running = p?.is_running === true;
      const stale   = p?.stale_symbols ?? 0;
      updated.data = { ...updated.data,
        status: running ? 'PASS' : 'FAIL',
        detail: running ? (stale > 0 ? `${stale} stale symbols` : '') : 'Pipeline not running',
      };
    } else {
      updated.data = { ...updated.data, status: 'FAIL', detail: 'Cannot reach pipeline API' };
    }

    if (rPos.status === 'fulfilled') {
      const pos = Array.isArray(rPos.value.data)
        ? rPos.value.data
        : (rPos.value.data?.positions ?? []);
      updated.position = { ...updated.position,
        status: pos.length === 0 ? 'PASS' : 'FAIL',
        detail: pos.length > 0 ? `${pos.length} open` : '',
      };
    }

    // Market hours: authoritative from backend preflight (trading_calendar check).
    if (rPreflight.status === 'fulfilled') {
      const calCheck = (rPreflight.value.data?.checks ?? [])
        .find(c => c.name === 'trading_calendar');
      if (calCheck) {
        updated.market = { ...updated.market,
          status: calCheck.status === 'PASS' ? 'PASS' : 'FAIL',
          detail: calCheck.status !== 'PASS' ? calCheck.message : '',
        };
      } else {
        updated.market = { ...updated.market, status: 'FAIL', detail: 'Preflight check missing' };
      }
    } else {
      updated.market = { ...updated.market, status: 'FAIL', detail: 'Cannot reach preflight API' };
    }

    setChecks(updated);
  };

  const openArmDialog = async () => {
    setShowArm(true);
    await runChecks();
  };

  const doArm = async () => {
    setArming(true);
    setArmMsg(null);
    try {
      await execApi.setMode('LIVE');
      await fetchAll();
      setArmMsg({ ok: true, text: 'LIVE mode armed. All orders will go to real broker.' });
      setShowArm(false);
    } catch (e) {
      setArmMsg({ ok: false, text: e.response?.data?.detail || e.message });
    } finally {
      setArming(false);
    }
  };

  const doPaper = async () => {
    try {
      await execApi.setMode('PAPER');
      await fetchAll();
      setArmMsg({ ok: true, text: 'Switched to PAPER mode. No real orders.' });
    } catch (e) {
      setArmMsg({ ok: false, text: e.response?.data?.detail || e.message });
    }
  };

  const isLive       = execData?.execution_mode === 'LIVE';
  const activeBroker = brokerData?.active_broker?.toUpperCase() ?? '—';
  const rd           = riskData;
  const riskLabel    = !rd                       ? '—'
    : rd.emergency_halt_active                   ? 'HALTED'
    : rd.loss_ceiling_breached                   ? 'CEILING BREACHED'
    : rd.cooldown_active                         ? 'COOLDOWN'
    :                                              'CLEAR';

  if (initialLoad) {
    return (
      <div>
        <PageHeader
          title="Execution"
          subtitle="06 — PAPER / LIVE mode and order routing. Backend is authoritative."
        />
        <LoadingBlock />
      </div>
    );
  }

  if (!execData && err) {
    return (
      <div>
        <PageHeader
          title="Execution"
          subtitle="06 — PAPER / LIVE mode and order routing. Backend is authoritative."
        />
        <ErrorBlock label="EXECUTION MODE UNAVAILABLE" detail={err} />
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="Execution"
        subtitle="06 — PAPER / LIVE mode and order routing. Backend is authoritative."
      />

      {showArm && (
        <LiveArmingModal
          checks={checks}
          onArm={doArm}
          onCancel={() => setShowArm(false)}
          arming={arming}
        />
      )}

      {armMsg && (
        <div className={`mb-4 px-4 py-2 rounded border text-[12px] font-mono ${
          armMsg.ok
            ? 'bg-green-950/20 border-green-800/30 text-green-300'
            : 'bg-red-950/20  border-red-800/40  text-red-300'
        }`}>
          {armMsg.text}
        </div>
      )}

      {err && execData && (
        <div className="mb-4 px-4 py-2 rounded border border-amber-700/40 bg-amber-950/20 text-amber-300 text-[12px] font-mono">
          Refresh error — showing last known state. {err}
        </div>
      )}

      <div className="space-y-4">

        {/* Mode banner */}
        <Panel>
          <div className="p-6 text-center">
            <div className={`text-[28px] font-bold font-mono mb-1 ${isLive ? 'text-red-400' : 'text-amber-300'}`}>
              {isLive ? 'LIVE MODE' : 'PAPER MODE'}
            </div>
            <div className="text-[12px] text-[#8D98A8] mb-2 font-mono">
              {execData?.description ?? (isLive
                ? 'Real broker execution. Orders sent to Dhan. Real capital at risk.'
                : 'Virtual execution only. Live market data, virtual money.')}
            </div>
            <div className="flex items-center justify-center gap-4 mb-4 text-[11px] font-mono text-[#4A5568]">
              {refreshing && <span>refreshing…</span>}
              {ageMs && <Freshness ageMs={ageMs} warnMs={STALE_WARN_MS} errMs={STALE_ERR_MS} />}
            </div>
            {isOperator ? (
              <div className="flex items-center justify-center gap-3">
                {isLive ? (
                  <ActionBtn color="amber" onClick={doPaper}>Switch to PAPER</ActionBtn>
                ) : (
                  <ActionBtn color="danger" onClick={openArmDialog}>
                    Arm LIVE Execution →
                  </ActionBtn>
                )}
              </div>
            ) : (
              <div className="text-[11px] font-mono text-[#4A5568]">
                Operator role required to change execution mode
              </div>
            )}
          </div>
        </Panel>

        {/* Execution status */}
        <Panel>
          <PanelHeader title="EXECUTION STATUS" subtitle="Live state from backend" />
          <div className="p-4 grid grid-cols-2 gap-3">
            <KV label="EXECUTION MODE"  value={execData?.execution_mode ?? '—'} />
            <KV label="ACTIVE BROKER"   value={activeBroker} />
            <KV label="RISK STATUS"     value={riskLabel} />
            <KV label="OPEN POSITIONS"  value={String(positions.length)} />
          </div>
        </Panel>

        {/* Execution candidates */}
        <Panel>
          <PanelHeader
            title="EXECUTION CANDIDATES"
            subtitle="Signals approved for execution by strategy engine"
          />
          <div className="p-4">
            {signals.length === 0 ? (
              <EmptyBlock label="No signals currently approved for execution" />
            ) : (
              <div>
                {signals.map((s, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-3 py-2 border-b border-[#1E2A3A] last:border-0 text-[12px] font-mono"
                  >
                    <StatusBadge text={s.side} color={s.side === 'BUY' ? 'green' : 'red'} />
                    <span className="text-[#F5F7FA] flex-1">{s.symbol}</span>
                    {s.category && (
                      <span className="text-[#4A5568] text-[10px]">{s.category}</span>
                    )}
                    {s.strength != null && (
                      <span className="text-[#8D98A8]">str {Number(s.strength).toFixed(2)}</span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </Panel>

        {/* Open positions */}
        <Panel>
          <PanelHeader title="OPEN POSITIONS" subtitle="Current positions in execution layer" />
          <div className="p-4">
            {positions.length === 0 ? (
              <EmptyBlock label="No open positions" />
            ) : (
              <div>
                {positions.map((p, i) => (
                  <div
                    key={i}
                    className="flex items-center gap-3 py-2 border-b border-[#1E2A3A] last:border-0 text-[12px] font-mono"
                  >
                    <span className="text-[#F5F7FA] flex-1">{p.symbol}</span>
                    <span className={p.quantity > 0 ? 'text-green-300' : 'text-red-400'}>
                      {p.quantity > 0 ? 'LONG' : 'SHORT'} {Math.abs(p.quantity)}
                    </span>
                    {p.avg_price != null && (
                      <span className="text-[#8D98A8]">avg {Number(p.avg_price).toFixed(2)}</span>
                    )}
                    {p.unrealized_pnl != null && (
                      <span className={p.unrealized_pnl >= 0 ? 'text-green-300' : 'text-red-400'}>
                        {p.unrealized_pnl >= 0 ? '+' : ''}{Number(p.unrealized_pnl).toFixed(2)}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </Panel>

        {/* Paper account — PAPER mode only */}
        {!isLive && paperAccount && (
          <Panel>
            <PanelHeader title="PAPER ACCOUNT" subtitle="Virtual account state" />
            <div className="p-4 grid grid-cols-2 gap-3">
              <KV label="STARTING CASH"  value={`₹${Number(paperAccount.starting_cash  ?? 0).toLocaleString('en-IN')}`} />
              <KV label="CASH AVAILABLE" value={`₹${Number(paperAccount.cash           ?? 0).toLocaleString('en-IN')}`} />
              <KV label="REALIZED P&L"   value={`₹${Number(paperAccount.realized_pnl   ?? 0).toLocaleString('en-IN')}`} />
              <KV label="UNREALIZED P&L" value={`₹${Number(paperAccount.unrealized_pnl ?? 0).toLocaleString('en-IN')}`} />
              <KV label="OPEN POSITIONS" value={String(paperAccount.open_positions ?? 0)} />
              <KV label="TOTAL FILLS"    value={String(paperAccount.fill_count     ?? 0)} />
            </div>
          </Panel>
        )}

        {/* Live execution gate reference */}
        <Panel>
          <PanelHeader title="LIVE EXECUTION GATES" subtitle="LIVE is blocked if any gate fails" />
          <div className="p-4 space-y-1">
            {[
              'Broker connected and authenticated',
              'Token valid (not expired)',
              'Market hours (backend: 09:15–15:30 IST weekdays)',
              'Risk not halted',
              'No cooldown active',
              'Market data pipeline running',
              'No reconciliation mismatch',
              'Strategy active',
              'Contract identity available (dhan_security_id)',
            ].map((item, i) => (
              <div key={i} className="flex items-center gap-2 py-1.5 border-b border-[#1E2A3A] last:border-0 text-[12px]">
                <span className="text-[#4A5568] text-[10px] w-3">{i + 1}</span>
                <span className="text-[#F5F7FA]">{item}</span>
              </div>
            ))}
          </div>
        </Panel>

        {/* Execution pipeline diagram */}
        <Panel>
          <PanelHeader title="EXECUTION PIPELINE" subtitle="Signal → Order → Fill → Position" />
          <div className="p-4">
            <div className="flex flex-col gap-0">
              {[
                'SIGNAL', 'HARD GATES', 'RISK', 'EXECUTION GATE',
                'BROKER', 'ORDER', 'ACK', 'FILL', 'POSITION',
              ].map((step, i, arr) => (
                <div key={step} className="flex flex-col items-center">
                  <div className="flex items-center gap-3 px-4 py-2 bg-[#151C25] border border-[#1E2A3A] rounded w-full max-w-sm mx-auto text-[12px] font-mono text-[#F5F7FA]">
                    <span className="text-[#4A5568] text-[10px] w-4">{i + 1}</span>
                    {step}
                  </div>
                  {i < arr.length - 1 && (
                    <div className="text-[#4A5568] text-[16px] my-0.5">↓</div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </Panel>

      </div>
    </div>
  );
}
