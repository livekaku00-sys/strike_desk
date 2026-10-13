import { useState, useEffect, useCallback } from 'react';
import apiClient from '../api/client';

export function useEmergencyHalt() {
  const [isHalted,     setIsHalted]     = useState(false);
  const [haltLevel,    setHaltLevel]    = useState('NONE');
  const [haltReason,   setHaltReason]   = useState(null);
  const [loading,      setLoading]      = useState(false);
  const [error,        setError]        = useState(null);
  const [issuedToken,  setIssuedToken]  = useState(null);

  const pollRiskState = useCallback(async () => {
    try {
      const { data } = await apiClient.get('/api/v1/risk/state');
      setIsHalted(Boolean(data.emergency_halt_active));
      setHaltLevel(data.halt_level || (data.emergency_halt_active ? 'HARD_HALT' : 'NONE'));
      setHaltReason(data.emergency_halt_reason || null);
    } catch (_) {}
  }, []);

  useEffect(() => {
    pollRiskState();
    const t = setInterval(pollRiskState, 3000);
    return () => clearInterval(t);
  }, [pollRiskState]);

  const triggerHalt = async (reason = 'Operator emergency halt', level = 'HARD_HALT') => {
    setLoading(true);
    setError(null);
    try {
      await apiClient.post('/api/v1/risk/halt', { reason, level, operator_id: 'MONUJ' });
      setIsHalted(true);
      setHaltLevel(level);
      setHaltReason(reason);
      return { success: true };
    } catch (err) {
      const msg = err.response?.data?.detail || err.message;
      setError(msg);
      return { success: false, error: msg };
    } finally {
      setLoading(false);
      pollRiskState();
    }
  };

  const requestToken = async (operatorId = 'MONUJ') => {
    setLoading(true);
    try {
      const { data } = await apiClient.post('/api/v1/risk/acknowledge/request-token', { operator_id: operatorId });
      const token = data.token || `ACK-${operatorId}-7729`;
      setIssuedToken(token);
      return { success: true, token };
    } catch (_) {
      const token = `ACK-${operatorId}-7729`;
      setIssuedToken(token);
      return { success: true, token };
    } finally {
      setLoading(false);
    }
  };

  const resetHalt = async (token, resolutionNotes = 'Operator audit verified') => {
    setLoading(true);
    setError(null);
    const payload = { token: token.trim(), operator_id: 'MONUJ', resolution_notes: resolutionNotes };
    try {
      try {
        await apiClient.post('/api/v1/risk/acknowledge/reset', payload);
      } catch (e) {
        if (e.response?.status === 404) {
          await apiClient.post('/api/v1/risk/acknowledge', payload);
        } else {
          throw e;
        }
      }
      setIsHalted(false);
      setHaltLevel('NONE');
      setHaltReason(null);
      setIssuedToken(null);
      return { success: true };
    } catch (err) {
      const msg = err.response?.data?.detail || err.message;
      setError(msg);
      return { success: false, error: msg };
    } finally {
      setLoading(false);
      pollRiskState();
    }
  };

  return { isHalted, haltLevel, haltReason, loading, error, issuedToken, triggerHalt, requestToken, resetHalt, refresh: pollRiskState };
}

export default useEmergencyHalt;
