"""
Unconditional guard against live broker order dispatch.

Live order placement, modification and cancellation are permanently prohibited
in this build. No execution mode, environment variable, configuration flag,
operator action or runtime state can suppress this restriction.

Import LiveOrderProhibitedError and raise it as the first statement of every
broker adapter method that would submit, modify or cancel a live order.
"""


class LiveOrderProhibitedError(RuntimeError):
    """
    Raised unconditionally when any code path attempts to place, modify or
    cancel a live broker order.

    This exception is not catchable as a normal operational error — callers
    must not retry, fall back or treat it as a transient failure. It signals
    a permanent architectural boundary, not a network or auth problem.
    """

    def __init__(self, context: str = "") -> None:
        msg = (
            "Live broker order dispatch is permanently prohibited. "
            "This build does not support live trading. "
            "No execution mode, configuration flag or operator action can "
            "enable live order submission."
        )
        if context:
            msg = f"{msg} [{context}]"
        super().__init__(msg)


def prohibit_live_order(context: str = "") -> None:
    """
    Raise LiveOrderProhibitedError unconditionally.

    Call this as the first statement in any broker adapter method that would
    otherwise submit, modify or cancel a live order. Accepts an optional
    context string (broker name, method name) for log clarity.
    """
    raise LiveOrderProhibitedError(context)
