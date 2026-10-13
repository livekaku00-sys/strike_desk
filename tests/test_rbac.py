"""
Role-based access control for state-changing endpoints.

Guests may read; only the operator may trade, change the execution lock, or touch
broker and risk administration. Unauthenticated callers get 401, read-only sessions
get 403 — the distinction matters because the web client force-logs-out on 401.
"""
import os
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from fastapi.testclient import TestClient

from api.main import create_app
from security.auth import (
    ROLE_GUEST,
    ROLE_OPERATOR,
    create_access_token,
    create_guest_token,
    verify_access_token,
)

# (method, path, body) for every endpoint that changes state.
MUTATING_ENDPOINTS = [
    ("post", "/api/v1/trading/orders", {"symbol": "NIFTY", "side": "BUY", "quantity": 1}),
    ("post", "/api/v1/trading/mode", {"mode": "AUTO_OPTIONS"}),
    ("post", "/api/v1/trading/positions/NIFTY/square-off", None),
    ("post", "/api/v1/brokers/active", {"broker": "dhan"}),
    ("post", "/api/v1/brokers/angelone/refresh", {}),
    ("post", "/api/v1/brokers/dhan/reauth", {"access_token": "x"}),
    ("post", "/api/v1/brokers/fyers/reauth", {"access_token": "x"}),
    ("post", "/api/v1/brokers/dhan/disconnect", None),
    ("post", "/api/v1/risk/halt", {"reason": "t", "operator_id": "monuj", "level": "HARD_HALT"}),
    ("post", "/api/v1/risk/acknowledge/request-token", {"operator_id": "monuj"}),
    ("post", "/api/v1/risk/acknowledge/reset", {"token": "x", "operator_id": "monuj"}),
]

READ_ENDPOINTS = [
    "/api/v1/trading/signals",
    "/api/v1/trading/orders",
    "/api/v1/trading/positions",
    "/api/v1/trading/mode",
    "/api/v1/risk/state",
]


class TestRBAC(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(create_app())
        cls.guest = create_guest_token()
        cls.operator = create_access_token("monuj", role=ROLE_OPERATOR)

    def _call(self, method, path, body, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return getattr(self.client, method)(path, json=body, headers=headers)

    def test_a_unauthenticated_mutations_are_401(self):
        for method, path, body in MUTATING_ENDPOINTS:
            with self.subTest(path=path):
                self.assertEqual(self._call(method, path, body).status_code, 401)

    def test_b_guest_mutations_are_403(self):
        for method, path, body in MUTATING_ENDPOINTS:
            with self.subTest(path=path):
                r = self._call(method, path, body, token=self.guest)
                self.assertEqual(r.status_code, 403, f"{path} did not reject a guest")

    def test_c_operator_passes_the_auth_gate(self):
        # The gate must let the operator through. What happens afterwards (risk
        # rejection, validation, a stale acknowledgement token) belongs to another
        # layer, so only an *authorization* refusal counts as a failure here —
        # /risk/acknowledge/reset answers 403 on a bad ack token of its own accord.
        for method, path, body in MUTATING_ENDPOINTS:
            with self.subTest(path=path):
                r = self._call(method, path, body, token=self.operator)
                self.assertNotEqual(r.status_code, 401, f"{path} rejected the operator's token")
                if r.status_code == 403:
                    detail = str(r.json().get("detail") or r.json().get("message") or "")
                    self.assertNotIn("session", detail.lower(),
                                     f"{path} refused the operator on authorization grounds")

    def test_d_guests_may_read(self):
        for path in READ_ENDPOINTS:
            with self.subTest(path=path):
                r = self.client.get(path, headers={"Authorization": f"Bearer {self.guest}"})
                self.assertEqual(r.status_code, 200)

    def test_e_guest_token_carries_no_operator_identity(self):
        payload = verify_access_token(self.guest)
        self.assertEqual(payload["role"], ROLE_GUEST)
        self.assertEqual(payload["sub"], "guest")

    def test_f_forged_operator_role_is_rejected(self):
        # A guest-subject token claiming the operator role must not verify, so
        # editing a persisted client-side role buys nothing.
        forged = create_access_token("guest", role=ROLE_OPERATOR)
        self.assertIsNone(verify_access_token(forged))

    def test_g_tampered_signature_is_rejected(self):
        head, payload, sig = self.operator.split(".")
        tampered = f"{head}.{payload}.{'A' * len(sig)}"
        self.assertIsNone(verify_access_token(tampered))
        r = self._call("post", "/api/v1/trading/mode", {"mode": "AUTO_OPTIONS"}, token=tampered)
        self.assertEqual(r.status_code, 401)

    def test_h_guest_login_endpoint_issues_a_readonly_token(self):
        r = self.client.post("/api/v1/auth/guest")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["user"]["role"], ROLE_GUEST)
        self.assertEqual(verify_access_token(body["access_token"])["role"], ROLE_GUEST)

    def test_i_simulation_is_explicit_never_a_fallback(self):
        """
        Simulation is legitimate, but only when PAPER is deliberately armed and the
        order is routed to the paper ledger. What must never come back is a silent
        local fill standing in for a failed broker call — that is what once made a
        live order look placed when it never left the process.
        """
        src = open(os.path.join(PROJECT_ROOT, "api", "routes", "trading.py")).read()
        for banned in ("PAPER_PLACED", "paper_fallback", "paper-mode fallback"):
            self.assertNotIn(banned, src, f"silent fill fallback reintroduced: {banned}")

        # A broker failure must still be recorded REJECTED and re-raised.
        self.assertIn('"status": "REJECTED"', src)
        self.assertIn("HTTP_502_BAD_GATEWAY", src)

    def test_j_live_mode_never_routes_to_the_paper_ledger(self):
        from api.routes.trading import EXEC_LIVE, ExecutionModeController
        ctrl = ExecutionModeController()
        ctrl.set(EXEC_LIVE)
        self.assertFalse(ctrl.is_paper())
        # And an unset controller must default to the harmless side.
        self.assertTrue(ExecutionModeController().is_paper())


if __name__ == "__main__":
    unittest.main()
