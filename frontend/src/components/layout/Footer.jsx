import React from 'react';
import EmergencyHalt from '../EmergencyHalt';
import { useCanTrade } from '../../store/authStore';
import { useSystemState } from '../../context/SystemStateContext';
import { REQ } from '../../utils/requestState';
import { requestStatusText } from '../../utils/stateLabels';

// Trade count from the shared /risk/state poll (risk_manager's in-memory count).
// A failed refresh keeps the last value but labels it STALE.
function tradeCountText(risk) {
  const v = risk?.value;
  if (!v) return requestStatusText(risk?.status ?? REQ.LOADING);
  const text = `${v.trade_count ?? '—'}/${v.max_daily_trades ?? '—'}`;
  return risk.status === REQ.OK ? text : `${text} STALE (${requestStatusText(risk.status)})`;
}

export default function Footer() {
  const canTrade = useCanTrade();
  const system = useSystemState();
  const trades = tradeCountText(system?.risk);

  return (
    <footer className="h-14 flex-shrink-0 bg-slate-950 border-t border-slate-800 flex items-center justify-between px-6">
      {/* Left: Emergency Halt + market status */}
      <div className="flex items-center gap-4">
        {canTrade && <EmergencyHalt />}
        {/* The backend has no market/session endpoint; this is not computed locally. */}
        <span
          className="text-xs font-mono text-slate-300"
          title="The backend does not expose market/session status to the dashboard."
        >
          Market status: <span className="font-semibold">NOT EXPOSED</span>
        </span>
      </div>

      {/* Right: trade count + EOD square-off */}
      <div className="flex items-center gap-4 text-xs font-mono text-slate-300">
        {canTrade && (
          <>
            <span aria-label={`Trades today: ${trades}`}>
              Trades: <span className="text-slate-100 font-semibold">{trades}</span>
            </span>
            <span className="text-slate-600" aria-hidden="true">|</span>
          </>
        )}
        <span title="No backend scheduler status for an end-of-day square-off is exposed to the dashboard.">
          EOD square-off: <span className="font-semibold">NOT EXPOSED</span>
        </span>
      </div>
    </footer>
  );
}
