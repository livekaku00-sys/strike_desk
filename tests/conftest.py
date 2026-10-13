import atexit
import os
import shutil
import tempfile

# pytest loads this before importing any test module, so every module-level singleton
# below is constructed against scratch state. Without it, test runs wrote the live
# broker tokens, the execution class lock, the risk DB (trade counts, P&L), the halt
# state, the audit log and the traded-contract registry.
_scratch = tempfile.mkdtemp(prefix="strike_desk_test_")
atexit.register(shutil.rmtree, _scratch, ignore_errors=True)

_REDIRECTS = {
    "STRIKE_DESK_ENV_FILE": "test.env",                      # brokers/token_manager.py
    "STRIKE_DESK_MODE_FILE": "execution_class_lock.json",    # api/routes/trading.py
    "STRIKE_DESK_CONTRACT_FILE": "contract_registry.json",   # execution/contract_store.py
    "STRIKE_DESK_RISK_DB": "risk_state.db",                  # risk/risk_state_manager.py
    "STRIKE_DESK_RISK_BACKUP": "risk_state_backup.json",     # risk/risk_state_manager.py
    "STRIKE_DESK_HALT_FILE": "halt_state.json",              # risk/emergency_halt.py
    "AUDIT_DB_PATH": "audit.db",                             # execution/audit_logger.py
}
for _var, _name in _REDIRECTS.items():
    os.environ[_var] = os.path.join(_scratch, _name)
