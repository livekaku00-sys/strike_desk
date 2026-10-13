import React, { useState, useEffect } from 'react';
import apiClient from '../api/client';
import { useCanTrade } from '../store/authStore';

const CLASSES = [
  { id: 'AUTO_CASH_INTRADAY', label: 'AUTO_CASH_INTRADAY', desc: 'Equity Intraday (ORB / Reversion)' },
  { id: 'AUTO_CASH_SWING', label: 'AUTO_CASH_SWING', desc: 'Equity Swing (Multi-Day Breakout)' },
  { id: 'AUTO_STOCK_FUTURES', label: 'AUTO_STOCK_FUTURES', desc: 'Stock Futures (Trend Directional)' },
  { id: 'AUTO_INDEX_FUTURES', label: 'AUTO_INDEX_FUTURES', desc: 'Index Futures (Benchmark Contango)' },
  { id: 'AUTO_INDEX_OPTIONS', label: 'AUTO_INDEX_OPTIONS', desc: 'Index Options (High Gamma Longs)' },
];

export const AssetClassSelector = ({ currentMode, onModeChange }) => {
  const canTrade = useCanTrade();
  const [selectedClass, setSelectedClass] = useState(currentMode || 'AUTO_CASH_INTRADAY');
  const [cascadeWarning, setCascadeWarning] = useState(false);
  const [updating, setUpdating] = useState(false);

  useEffect(() => {
    if (currentMode) {
      setSelectedClass(currentMode);
    }
  }, [currentMode]);

  // Changing the execution lock decides which class routes live orders, so it is
  // operator-only and must go through apiClient to carry the session token.
  const handleSelect = async (classId) => {
    if (!canTrade || updating) return;
    const previous = selectedClass;
    setSelectedClass(classId);
    setUpdating(true);
    try {
      await apiClient.post('/api/v1/trading/mode', { mode: classId });
      if (onModeChange) onModeChange(classId);
    } catch (e) {
      // The lock lives on the server. Reverting keeps the UI from showing a change
      // the backend refused.
      setSelectedClass(previous);
      console.warn('[AssetClassSelector] Mode change rejected:', e.response?.data?.detail || e.message);
    } finally {
      setUpdating(false);
    }
  };

  return (
    <div className="w-full bg-[#131824] p-3.5 rounded-lg border border-slate-800 text-slate-200">
      <div className="flex items-center justify-between mb-2.5">
        <span className="text-[11px] font-bold uppercase tracking-wider text-sky-400 font-mono">
          ⚡ Asset Class Selector (O8)
        </span>
        {updating && <span className="text-[10px] font-mono text-sky-400 animate-pulse">LOCKING...</span>}
        {!canTrade && !updating && (
          <span className="text-[10px] font-mono text-slate-500">READ-ONLY</span>
        )}
      </div>

      {/* Cascade Brake Alert Indicator */}
      {cascadeWarning && (
        <div className="mb-3 bg-amber-500/15 border border-amber-500/40 p-2 rounded text-[11px] text-amber-300 font-mono flex items-center gap-1.5">
          <span>⚠️</span> Cascade Brake: 3+ classes signaled in 30s. Position sizes scaled 50%.
        </div>
      )}

      {/* 5 Radio Buttons */}
      <div className="space-y-2 font-mono text-xs">
        {CLASSES.map((cls) => {
          const isSelected = selectedClass === cls.id;

          return (
            <label
              key={cls.id}
              onClick={() => handleSelect(cls.id)}
              className={`flex items-center justify-between p-2 rounded transition border ${
                canTrade ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'
              } ${
                isSelected
                  ? 'bg-emerald-950/20 border-emerald-500/50 text-slate-100'
                  : 'bg-slate-900/50 border-slate-800/80 text-slate-400 hover:bg-slate-800/40 hover:text-slate-200'
              }`}
            >
              <div className="flex items-center gap-2.5">
                <input
                  type="radio"
                  name="asset_class_mode"
                  value={cls.id}
                  checked={isSelected}
                  onChange={() => {}}
                  disabled={!canTrade}
                  className="accent-emerald-500 disabled:cursor-not-allowed"
                />
                <div>
                  <div className="font-bold text-[11px] text-slate-200">{cls.label}</div>
                  <div className="text-[10px] text-slate-500">{cls.desc}</div>
                </div>
              </div>

              <div>
                {isSelected ? (
                  <span className="px-1.5 py-0.2 rounded text-[9px] font-black bg-emerald-500/20 text-emerald-400 border border-emerald-500/40">
                    EXECUTING
                  </span>
                ) : (
                  <span className="px-1.5 py-0.2 rounded text-[9px] font-bold bg-amber-500/10 text-amber-400/80 border border-amber-500/20">
                    ALERT_ONLY
                  </span>
                )}
              </div>
            </label>
          );
        })}
      </div>

      <div className="mt-3 pt-2 border-t border-slate-800 text-[10px] text-slate-500 font-mono leading-tight">
        Enforces Blueprint O8 ONE-CATEGORY Rule. Zero order leakage to non-selected classes.
      </div>
    </div>
  );
};

export default AssetClassSelector;
