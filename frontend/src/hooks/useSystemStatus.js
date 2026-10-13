/**
 * Strike Desk V4.5 — System Status Hook
 * Polls all critical-path endpoints every N seconds.
 * This is the authoritative source for the TopBar and sidebar badges.
 * Frontend NEVER derives state; it only displays what the backend reports.
 */
import { useState, useEffect, useCallback, useRef } from 'react';
import { brokers, risk, execution, strategy, market } from '../api/services';

// IST market-hours check — supplementary fallback when backend has no explicit field
function deriveMarketOpen() {
  const ist = new Date(new Date().toLocaleString('en-US', { timeZone: 'Asia/Kolkata' }));
  const day = ist.getDay(); // 0=Sun 6=Sat
  if (day === 0 || day === 6) return false;
  const hhmm = ist.getHours() * 100 + ist.getMinutes();
  return hhmm >= 915 && hhmm < 1530;
}

const FAST_POLL   = 5_000;   // risk state, execution mode, positions
const MEDIUM_POLL = 15_000;  // brokers, signals, watchlist
const SLOW_POLL   = 30_000;  // instruments count

function ts() { return Date.now(); }

const INIT = { data: null, loading: true, error: null, age: null };

export function useSystemStatus() {
  const [execMode,    setExecMode]    = useState(INIT);
  const [riskState,   setRiskState]   = useState(INIT);
  const [brokersData, setBrokersData] = useState(INIT);
  const [activeBroker,setActiveBroker]= useState(INIT);
  const [signalCount, setSignalCount] = useState(null);
  const [queueCount,  setQueueCount]  = useState(null);
  const [posCount,    setPosCount]    = useState(null);
  const [orderCount,  setOrderCount]  = useState(null);
  const [mktStatus,   setMktStatus]   = useState(INIT);
  const [stratMode,   setStratMode]   = useState(INIT);

  // Track mount state to prevent setState after unmount
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);

  const safe = (fn) => (...args) => { if (alive.current) fn(...args); };

  const fetchFast = useCallback(async () => {
    const [rExec, rRisk, rPos, rOrders] = await Promise.allSettled([
      execution.mode(),
      risk.state(),
      execution.positions(),
      execution.orders(),
    ]);

    if (rExec.status === 'fulfilled') {
      safe(setExecMode)({ data: rExec.value.data?.mode, loading: false, error: null, age: ts() });
    } else {
      safe(setExecMode)(p => ({ ...p, loading: false, error: rExec.reason?.message || 'error', age: p.age }));
    }

    if (rRisk.status === 'fulfilled') {
      safe(setRiskState)({ data: rRisk.value.data, loading: false, error: null, age: ts() });
    } else {
      safe(setRiskState)(p => ({ ...p, loading: false, error: rRisk.reason?.message || 'error', age: p.age }));
    }

    if (rPos.status === 'fulfilled') {
      const positions = rPos.value.data?.positions ?? rPos.value.data ?? [];
      safe(setPosCount)(Array.isArray(positions) ? positions.length : null);
    }

    if (rOrders.status === 'fulfilled') {
      const orders = rOrders.value.data?.orders ?? rOrders.value.data ?? [];
      const pending = Array.isArray(orders)
        ? orders.filter(o => !['FILLED','CANCELLED','REJECTED'].includes(o.status)).length
        : null;
      safe(setOrderCount)(pending);
    }
  }, []);

  const fetchMedium = useCallback(async () => {
    const [rBrokers, rActive, rSig, rQueue, rMkt, rStrat] = await Promise.allSettled([
      brokers.tokensStatus(),
      brokers.active(),
      strategy.signals(),
      strategy.queue(),
      market.ticks(),
      strategy.mode(),
    ]);

    if (rBrokers.status === 'fulfilled') {
      safe(setBrokersData)({ data: rBrokers.value.data?.brokers ?? [], loading: false, error: null, age: ts() });
    } else {
      safe(setBrokersData)(p => ({ ...p, loading: false, error: rBrokers.reason?.message || 'error', age: p.age }));
    }

    if (rActive.status === 'fulfilled') {
      safe(setActiveBroker)({ data: rActive.value.data?.active_broker ?? null, loading: false, error: null, age: ts() });
    } else {
      safe(setActiveBroker)(p => ({ ...p, loading: false, error: rActive.reason?.message || 'error', age: p.age }));
    }

    if (rSig.status === 'fulfilled') {
      const sigs = rSig.value.data?.signals ?? rSig.value.data ?? [];
      safe(setSignalCount)(Array.isArray(sigs) ? sigs.length : null);
    }

    if (rQueue.status === 'fulfilled') {
      const q = rQueue.value.data?.queue ?? rQueue.value.data ?? [];
      safe(setQueueCount)(Array.isArray(q) ? q.length : null);
    }

    if (rMkt.status === 'fulfilled') {
      safe(setMktStatus)({ data: rMkt.value.data, loading: false, error: null, age: ts() });
    } else {
      safe(setMktStatus)(p => ({ ...p, loading: false, error: rMkt.reason?.message || 'error', age: p.age }));
    }

    if (rStrat.status === 'fulfilled') {
      const m = rStrat.value.data?.mode ?? rStrat.value.data?.active_mode ?? null;
      safe(setStratMode)({ data: m, loading: false, error: null, age: ts() });
    } else {
      safe(setStratMode)(p => ({ ...p, loading: false, error: rStrat.reason?.message || 'error', age: p.age }));
    }
  }, []);

  useEffect(() => {
    fetchFast();
    fetchMedium();
    const t1 = setInterval(fetchFast,   FAST_POLL);
    const t2 = setInterval(fetchMedium, MEDIUM_POLL);
    return () => { clearInterval(t1); clearInterval(t2); };
  }, [fetchFast, fetchMedium]);

  // Derived readiness — never invented, always from backend fields
  const rd = riskState.data;
  const activeB = activeBroker.data;
  const brokersList = brokersData.data ?? [];
  const activeBrokerInfo = brokersList.find(b => b.broker_name === activeB);

  const blockers = [];
  if (!activeBrokerInfo?.is_authenticated)           blockers.push({ level: 'error',   msg: `${activeB ?? 'Broker'} token expired or missing` });
  if (rd?.emergency_halt_active)                      blockers.push({ level: 'error',   msg: 'Emergency halt is active' });
  if (rd?.loss_ceiling_breached)                      blockers.push({ level: 'error',   msg: 'Daily loss ceiling breached' });
  if (rd?.cooldown_active)                            blockers.push({ level: 'warning', msg: `Cooldown until ${rd.cooldown_until ?? '—'}` });
  if (rd?.trade_count >= 5)                           blockers.push({ level: 'warning', msg: 'Daily trade limit (5) reached' });

  const isHalted       = !!(rd?.emergency_halt_active);
  const isLive         = execMode.data === 'LIVE';
  const riskClear      = !isHalted && !rd?.loss_ceiling_breached;
  const brokerOk       = !!(activeBrokerInfo?.is_authenticated);
  const readyToTrade   = riskClear && brokerOk && !rd?.cooldown_active && (rd?.trade_count ?? 0) < 5;

  return {
    execMode,
    riskState,
    brokersData,
    activeBroker,
    mktStatus,
    stratMode,
    signalCount,
    queueCount,
    posCount,
    orderCount,
    isHalted,
    isLive,
    riskClear,
    brokerOk,
    readyToTrade,
    blockers,
    deriveMarketOpen,
    refetchFast:   fetchFast,
    refetchMedium: fetchMedium,
  };
}
