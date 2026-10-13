import React from 'react';
import { useOutletContext } from 'react-router-dom';
import AlertMatrix from '../components/AlertMatrix';
import TradingPanels from '../components/TradingPanels';
import { useIsGuest } from '../store/authStore';

export default function Dashboard() {
  const ctx = useOutletContext() || {};
  const activeMode = ctx.activeMode || 'AUTO_OPTIONS';
  const isGuest = useIsGuest();

  return (
    <div className="space-y-6">
      {isGuest && (
        <div className="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-3 text-xs text-slate-400 font-mono">
          <span className="font-bold text-slate-300">👁 Observation mode.</span>{' '}
          You are seeing the signals the bot generates and live market data. Positions,
          orders, risk state and broker routing are not part of a guest session.
        </div>
      )}

      <AlertMatrix activeMode={activeMode} />

      {/* Positions, orders, risk limits and system status are execution detail,
          so they are not rendered at all for an observation-only session. */}
      {!isGuest && <TradingPanels />}
    </div>
  );
}
