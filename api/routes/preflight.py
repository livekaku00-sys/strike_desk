"""
Pre-Flight Check API Routes (P0-02)

GET  /api/v1/preflight/status  — last report, or fresh run if none cached
POST /api/v1/preflight/run     — manual trigger (operator only)
"""
from typing import Any, Dict

from fastapi import APIRouter, Depends

from .auth import require_operator

router = APIRouter()


@router.get("/status")
async def get_preflight_status():
    """
    Returns the last pre-flight report.  Triggers a fresh run if no report
    has been cached yet (e.g. at startup before the 08:45 IST scheduler fires).
    Readable by any authenticated session — the dashboard always needs this.
    """
    from services.preflight import preflight_engine as _engine
    report = _engine.get_last_report()
    if report is None:
        report = _engine.run_all()
    return report.to_dict()


@router.post("/run")
async def run_preflight(_op: Dict[str, Any] = Depends(require_operator)):
    """
    Manual pre-flight trigger.  Operator access required so a guest session
    cannot spam check runs during a live session.
    """
    from services.preflight import preflight_engine as _engine
    return _engine.run_all().to_dict()
