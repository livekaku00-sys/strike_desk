/**
 * Strike Desk V4.5 — Navigation Sidebar
 * Collapsible professional nav with live status badges from systemStatus.
 */
import React, { useState } from 'react';
import { NavLink, useLocation } from 'react-router-dom';
import { useAuthStore } from '../../store/authStore';

function Badge({ count, color = 'gray' }) {
  if (count === null || count === undefined) return null;
  const cls = {
    green:  'bg-green-500/20 text-green-300 border-green-500/40',
    amber:  'bg-amber-500/20 text-amber-300 border-amber-500/40',
    red:    'bg-red-500/20   text-red-400   border-red-500/40 blink-danger',
    gray:   'bg-[#1E2A3A]   text-[#8D98A8] border-[#2A3F5A]',
  };
  return (
    <span className={`ml-auto text-[10px] font-mono font-bold px-1.5 py-0.5 rounded border ${cls[color]}`}>
      {count}
    </span>
  );
}

function NavGroup({ label, children }) {
  return (
    <div className="mb-4">
      <div className="px-3 mb-1.5 text-[9px] font-bold uppercase tracking-[0.12em] text-[#4A5568]">
        {label}
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  );
}

function NavItem({ to, label, badge, badgeColor, icon, alert }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `flex items-center gap-2.5 px-3 py-2 rounded text-[12px] font-medium transition-all duration-100 group ${
          isActive
            ? 'bg-[#151C25] text-[#F5F7FA] border-l-2 border-amber-400 pl-[10px]'
            : 'text-[#8D98A8] hover:text-[#F5F7FA] hover:bg-[#111821] border-l-2 border-transparent pl-[10px]'
        }`
      }
    >
      {icon && <span className="text-[13px] opacity-70">{icon}</span>}
      <span className="flex-1 truncate">{label}</span>
      {alert && (
        <span className="w-1.5 h-1.5 rounded-full bg-red-500 pulse-dot" title={alert} />
      )}
      {badge !== null && badge !== undefined && (
        <Badge count={badge} color={badgeColor} />
      )}
    </NavLink>
  );
}

export default function NavSidebar({ systemStatus, collapsed, onToggle }) {
  const { role, logout } = useAuthStore();
  const {
    signalCount,
    queueCount,
    posCount,
    orderCount,
    isHalted,
    isLive,
    activeBroker,
    brokersData,
    execMode,
    riskState,
  } = systemStatus ?? {};

  const rd = riskState?.data;
  const activeB = activeBroker?.data ?? '—';
  const brokersList = brokersData?.data ?? [];
  const activeBrokerInfo = brokersList.find(b => b.broker_name === activeB);
  const brokerOk = activeBrokerInfo?.is_authenticated;
  const mode = execMode?.data;

  return (
    <aside className={`flex-shrink-0 flex flex-col bg-[#0B0F14] border-r border-[#1E2A3A] transition-all duration-200 ${collapsed ? 'w-12' : 'w-52'}`}>

      {/* Collapse toggle */}
      <button
        onClick={onToggle}
        className="h-9 flex items-center justify-center border-b border-[#1E2A3A] text-[#4A5568] hover:text-[#8D98A8] transition-colors flex-shrink-0"
        title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
      >
        <span className="text-[11px] font-mono">{collapsed ? '›' : '‹'}</span>
      </button>

      {collapsed ? (
        /* Collapsed: icon-only hints */
        <div className="flex-1 flex flex-col items-center gap-1 py-3">
          {[
            { to: '/',          icon: '◉' },
            { to: '/brokers',   icon: '⬡' },
            { to: '/risk',      icon: '⚠' },
            { to: '/execution', icon: '▶' },
            { to: '/signals',   icon: '↗' },
            { to: '/positions', icon: '◈' },
            { to: '/orders',    icon: '⊟' },
            { to: '/audit',     icon: '▤' },
          ].map(({ to, icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `w-8 h-8 flex items-center justify-center rounded text-[14px] ${
                  isActive ? 'bg-[#151C25] text-[#F5F7FA]' : 'text-[#4A5568] hover:text-[#8D98A8]'
                }`
              }
            >
              {icon}
            </NavLink>
          ))}
        </div>
      ) : (
        /* Expanded nav */
        <nav className="flex-1 overflow-y-auto py-3 px-2">

          <NavGroup label="Overview">
            <NavItem to="/" label="Command Center" icon="◉" />
          </NavGroup>

          <NavGroup label="Workflow">
            <NavItem to="/auth"      label="01  Authentication" icon="🔐" />
            <NavItem to="/brokers"   label="02  Brokers"         icon="⬡"
              alert={!brokerOk ? 'Token expired or missing' : null}
              badge={!brokerOk ? '!' : null} badgeColor="red"
            />
            <NavItem to="/market"    label="03  Market Data"     icon="📡" />
            <NavItem to="/strategy"  label="04  Strategy Engine" icon="⚙" />
            <NavItem to="/risk"      label="05  Risk"            icon="⚠"
              alert={isHalted ? 'Emergency halt active' : null}
              badge={isHalted ? 'HALT' : null} badgeColor="red"
            />
            <NavItem to="/execution" label="06  Execution"       icon="▶"
              badge={isLive ? 'LIVE' : null} badgeColor="red"
            />
            <NavItem to="/modules"   label="07  Other Modules"   icon="◫" />
            <NavItem to="/checklist" label="08  Daily Checklist" icon="☑" />
          </NavGroup>

          <NavGroup label="Trading">
            <NavItem to="/signals"   label="Signals"    icon="↗"
              badge={signalCount} badgeColor={signalCount > 0 ? 'green' : 'gray'}
            />
            <NavItem to="/watchlist" label="Watchlist"  icon="◎" />
            <NavItem to="/queue"     label="Queue"      icon="⊞"
              badge={queueCount}  badgeColor={queueCount > 0 ? 'amber' : 'gray'}
            />
            <NavItem to="/positions" label="Positions"  icon="◈"
              badge={posCount}    badgeColor={posCount > 0 ? 'amber' : 'gray'}
            />
            <NavItem to="/orders"    label="Orders"     icon="⊟"
              badge={orderCount}  badgeColor={orderCount > 0 ? 'teal' : 'gray'}
            />
            <NavItem to="/trades"    label="Trades & P&L" icon="₹" />
          </NavGroup>

          <NavGroup label="System">
            <NavItem to="/audit"           label="Audit Log"       icon="▤" />
            <NavItem to="/system/health"   label="API Health"      icon="♡" />
            <NavItem to="/system/events"   label="Events"          icon="≋" />
            <NavItem to="/reconciliation"  label="Reconciliation"  icon="⇄" />
            <NavItem to="/configuration"   label="Configuration"   icon="⊕" />
          </NavGroup>

        </nav>
      )}

      {/* Bottom status strip */}
      {!collapsed && (
        <div className="flex-shrink-0 border-t border-[#1E2A3A] p-3 space-y-1.5">
          <div className="flex items-center justify-between text-[10px] font-mono">
            <span className="text-[#4A5568] uppercase">Execution</span>
            <span className={mode === 'LIVE' ? 'text-red-400 font-bold' : 'text-amber-300'}>
              {mode ?? '—'}
            </span>
          </div>
          <div className="flex items-center justify-between text-[10px] font-mono">
            <span className="text-[#4A5568] uppercase">Broker</span>
            <span className={brokerOk ? 'text-green-300' : 'text-red-400'}>
              {activeB.toUpperCase()}
            </span>
          </div>
          <div className="flex items-center justify-between text-[10px] font-mono">
            <span className="text-[#4A5568] uppercase">Risk</span>
            <span className={
              isHalted                ? 'text-red-400 blink-danger' :
              rd?.loss_ceiling_breached ? 'text-red-400' :
              rd?.cooldown_active     ? 'text-amber-300' :
              rd                      ? 'text-green-300' :
                                        'text-[#4A5568]'
            }>
              {isHalted                  ? '● HALTED' :
               rd?.loss_ceiling_breached ? '● CEILING' :
               rd?.cooldown_active       ? '● COOLDOWN' :
               rd                        ? '● CLEAR' :
                                           '● —'}
            </span>
          </div>
          <div className="flex items-center justify-between text-[10px] font-mono">
            <span className="text-[#4A5568] uppercase">Operator</span>
            <span className="text-[#8D98A8]">● {role === 'operator' ? 'ONLINE' : 'GUEST'}</span>
          </div>
          <button
            onClick={logout}
            className="w-full mt-1 py-1.5 text-[10px] font-bold uppercase tracking-wider text-[#4A5568] hover:text-red-400 border border-[#1E2A3A] hover:border-red-800/50 rounded transition-colors"
          >
            Logout
          </button>
        </div>
      )}
    </aside>
  );
}
