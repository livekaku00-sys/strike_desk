import React from 'react';
import EmergencyHalt from './EmergencyHalt';

export const Footer = () => {
  return (
    <footer className="h-12 border-t border-slate-800 bg-[#0C1018] px-4 md:px-6 flex items-center justify-between shrink-0 select-none text-xs text-slate-400 font-mono">
      {/* Left: Subsystem Latency & Architecture Status */}
      <div className="flex items-center gap-4">
        <span className="flex items-center gap-1.5 text-emerald-400">
          <span className="h-2 w-2 rounded-full bg-emerald-400 animate-pulse" />
          <span>FASTAPI 8765 [RMS ACTIVE]</span>
        </span>
        <span className="hidden sm:inline text-slate-600">|</span>
        <span className="hidden sm:inline text-slate-400">Latency: 12ms</span>
      </div>

      {/* Center: System Operational Banner */}
      <div className="hidden md:flex items-center gap-2 text-[11px]">
        <span className="text-slate-300 font-bold">STRIKE DESK QUANT V4</span>
        <span className="text-slate-600">•</span>
        <span>BLUEPRINT O8 ONE-CATEGORY LOCK</span>
      </div>

      {/* Right: Emergency Halt Component Button & Trigger */}
      <div className="flex items-center gap-2">
        <EmergencyHalt />
      </div>
    </footer>
  );
};

export default Footer;
