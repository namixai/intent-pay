"""Where an allowed decision becomes a sandbox call.

The agent hands a proposal to `Gateway.submit` and reads back `Outcome.for_agent()`: the
decision, the rule behind it, and what the sandbox said. The payer (the PayPal or Airwallex
adapter) is built from environment variables in this process. The agent's side never gets it,
and the payer is called only with an intent the engine allowed and wrote down. A refused
proposal makes no network call at all.

What the sandbox did is written to the same ledger as an execution record that points at the
decision. If a payment fails at the provider, the decision still counts against the day: the
engine reserved the amount when it said yes, and an error does not give it back.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol

from intent_pay.intent import Intent
from intent_pay.ledger import ALLOW, DECISION, Execution, Ledger, LedgerError, Receipt
from intent_pay.policy import Engine, utc_now


@dataclass(frozen=True)
class PaymentResult:
    provider: str
    operation: str
    ok: bool
    status: str
    reference: str | None
    detail: str
    steps: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "operation": self.operation, "ok": self.ok, "status": self.status,
                "reference": self.reference, "detail": self.detail, "steps": [dict(s) for s in self.steps]}


class Payer(Protocol):
    provider: str

    def pay(self, intent: Intent, receipt: Receipt) -> PaymentResult: ...

    def refund(self, reference: str, request_id: str) -> PaymentResult: ...


class GatewayError(Exception):
    """A request the gateway will not pass to the sandbox (for example, a refund of nothing)."""


@dataclass(frozen=True)
class Outcome:
    receipt: Receipt | None
    payment: PaymentResult | None = None
    execution: Execution | None = None
    note: str = ""

    def for_agent(self) -> dict[str, Any]:
        """What the agent may see: the decision and the sandbox's answer, nothing about keys."""
        out: dict[str, Any] = {}
        if self.receipt is not None:
            out.update(decision=self.receipt.decision, reason=self.receipt.reason, detail=self.receipt.detail,
                       request_id=self.receipt.request_id, receipt=self.receipt.seq)
        if self.payment is not None:
            out["payment"] = {"ok": self.payment.ok, "status": self.payment.status,
                              "reference": self.payment.reference, "detail": self.payment.detail}
        if self.note:
            out["note"] = self.note
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt": None if self.receipt is None else self.receipt.to_dict(),
            "payment": None if self.payment is None else self.payment.to_dict(),
            "execution": None if self.execution is None else self.execution.to_dict(),
            "note": self.note,
        }


class Gateway:
    def __init__(self, engine: Engine, payer: Payer, clock: Callable[[], datetime] = utc_now):
        self.engine = engine
        self.payer = payer
        self.clock = clock

    @property
    def ledger(self) -> Ledger:
        return self.engine.ledger

    def submit(self, proposal: Any) -> Outcome:
        receipt = self.engine.decide(proposal)
        if not receipt.allowed:
            return Outcome(receipt)
        intent = Intent.parse(receipt.intent)
        result = self._call(lambda: self.payer.pay(intent, receipt), "pay")
        return self._record(receipt, receipt.seq, result)

    def refund(self, decision_seq: int) -> Outcome:
        """Refund the payment made for an allowed decision. Refunds give money back, so they do
        not pass the policy; they only ever undo a payment this ledger recorded."""
        with self.ledger.session() as state:
            decision = state.record(decision_seq)
            if decision is None or decision["kind"] != DECISION or decision["decision"] != ALLOW:
                raise GatewayError(f"receipt #{decision_seq} is not an allowed decision")
            done = state.executions.get(decision_seq, [])
        payments = [e for e in done if e["operation"] == "pay" and e["ok"] and e["reference"]]
        if not payments:
            raise GatewayError(f"receipt #{decision_seq} has no completed payment to refund")
        if any(e["operation"] == "refund" and e["ok"] for e in done):
            raise GatewayError(f"receipt #{decision_seq} is already refunded")
        reference = payments[-1]["reference"]
        result = self._call(lambda: self.payer.refund(reference, decision["request_id"]), "refund")
        return self._record(None, decision_seq, result)

    def _call(self, fn: Callable[[], PaymentResult], operation: str) -> PaymentResult:
        try:
            return fn()
        except Exception as exc:  # a network error is an answer too, and it gets recorded
            return PaymentResult(provider=self.payer.provider, operation=operation, ok=False, status="ERROR",
                                 reference=None, detail=f"{type(exc).__name__}: {exc}")

    def _record(self, receipt: Receipt | None, decision_seq: int, result: PaymentResult) -> Outcome:
        try:
            with self.ledger.session() as state:
                execution = self.ledger.append_execution(
                    state, decision_seq=decision_seq, provider=result.provider, operation=result.operation,
                    ok=result.ok, status=result.status, reference=result.reference, detail=result.detail,
                    steps=result.steps, at=self.clock())
        except LedgerError as exc:
            return Outcome(receipt, result, None,
                           note=f"the sandbox call was made but its record could not be written: {exc}")
        return Outcome(receipt, result, execution)
