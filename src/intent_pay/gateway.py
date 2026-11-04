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

from intent_pay.intent import Intent, parse_money
from intent_pay.ledger import (ALLOW, APPROVAL, AWAIT_APPROVAL, DECISION, FORCED_CHARGE, Execution, Ledger,
                               LedgerError, Receipt)
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


def _paid(executions: list[dict[str, Any]]) -> list[str]:
    """The references of an allowed decision's payments that went through, oldest first."""
    return [e["reference"] for e in executions if e["operation"] == "pay" and e["ok"] and e["reference"]]


class GatewayError(Exception):
    """A request the gateway will not pass to the sandbox (for example, a refund of nothing)."""


def held_by(ledger: Ledger, decision_seq: int) -> str:
    """The provider whose payment for this receipt waits for approval, as the hold recorded it."""
    with ledger.session() as state:
        holds = [e for e in state.executions.get(decision_seq, []) if e["operation"] == AWAIT_APPROVAL]
    if not holds:
        raise GatewayError(f"receipt #{decision_seq} is not waiting for approval")
    return holds[-1]["provider"]


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

    def submit(self, proposal: Any, order: Any = None) -> Outcome:
        receipt = self.engine.decide(proposal, order)
        if not receipt.allowed:
            return Outcome(receipt)
        intent = Intent.parse(receipt.intent)
        threshold = self.engine.policy.currencies[intent.currency].approve_above
        if threshold is not None and intent.amount > threshold:
            # Within the limits but above what the owner lets run alone: hold it for a person.
            held = PaymentResult(provider=self.payer.provider, operation=AWAIT_APPROVAL, ok=False,
                                 status="AWAITING_APPROVAL", reference=None,
                                 detail=(f"{intent.amount} {intent.currency} is above the approval threshold of "
                                         f"{threshold} {intent.currency}; nothing is paid until a person approves "
                                         f"receipt #{receipt.seq}"))
            return self._record(receipt, receipt.seq, held)
        result = self._call(lambda: self.payer.pay(intent, receipt), "pay")
        return self._record(receipt, receipt.seq, result)

    def approve(self, decision_seq: int, by: str) -> Outcome:
        """A person's yes to a held payment, then the payment. Only a payment this gateway's provider
        held can be approved, and the yes is recorded once. If the payment then did not go through,
        or the process stopped before making it, the same call pays it; once a payment of the
        receipt went through, nothing more is paid. A retry carries the receipt's request ID, so
        the provider's own idempotency answers a payment it made but the ledger did not record.
        The agent has no tool that reaches this."""
        if not isinstance(by, str) or not by.strip():
            raise GatewayError("an approval needs the name of the person who gives it")
        with self.ledger.session() as state:
            decision = state.record(decision_seq)
            if decision is None or decision["kind"] != DECISION or decision["decision"] != ALLOW:
                raise GatewayError(f"receipt #{decision_seq} is not an allowed decision")
            done = state.executions.get(decision_seq, [])
            holds = [e for e in done if e["operation"] == AWAIT_APPROVAL]
            if not holds:
                raise GatewayError(f"receipt #{decision_seq} is not waiting for approval")
            if holds[-1]["provider"] != self.payer.provider:
                raise GatewayError(f"receipt #{decision_seq} is held for {holds[-1]['provider']}, "
                                   f"not for {self.payer.provider}")
            if _paid(done):
                raise GatewayError(f"receipt #{decision_seq} was already approved and paid")
            approvals = [e for e in done if e["operation"] == APPROVAL]
            if approvals:
                note = f"approved earlier as #{approvals[0]['seq']}; this call completes the payment"
            else:
                execution = self.ledger.append_execution(
                    state, decision_seq=decision_seq, provider="person", operation=APPROVAL, ok=True,
                    status="APPROVED", reference=None, detail=f"approved by {by.strip()}", steps=(),
                    at=self.clock())
                note = f"approval recorded as #{execution.seq}"
        receipt = Receipt(decision=decision["decision"], reason=decision["reason"], detail=decision["detail"],
                          request_id=decision["request_id"], intent=decision["intent"],
                          policy_hash=decision["policy_hash"], at=decision["at"], seq=decision["seq"],
                          prev=decision["prev"], hash=decision["hash"])
        intent = Intent.parse(receipt.intent)
        result = self._call(lambda: self.payer.pay(intent, receipt), "pay")
        outcome = self._record(receipt, decision_seq, result)
        return Outcome(outcome.receipt, outcome.payment, outcome.execution,
                       note=note + (f"; {outcome.note}" if outcome.note else ""))

    def refund(self, decision_seq: int) -> Outcome:
        """Refund the payment made for an allowed decision. Refunds give money back, so they do
        not pass the policy; they only ever undo a payment this ledger recorded."""
        with self.ledger.session() as state:
            decision = state.record(decision_seq)
            if decision is None or decision["kind"] != DECISION or decision["decision"] != ALLOW:
                raise GatewayError(f"receipt #{decision_seq} is not an allowed decision")
            done = state.executions.get(decision_seq, [])
        payments = _paid(done)
        if not payments:
            raise GatewayError(f"receipt #{decision_seq} has no completed payment to refund")
        if any(e["operation"] == "refund" and e["ok"] for e in done):
            raise GatewayError(f"receipt #{decision_seq} is already refunded")
        reference = payments[-1]
        result = self._call(lambda: self.payer.refund(reference, decision["request_id"]), "refund")
        return self._record(None, decision_seq, result)

    def force_charge(self, request_id: str, amount: Any, currency: str, merchant_category: str | None) -> Outcome:
        """Charge the card of an allowed payment directly, as a vendor holding the card could,
        without asking the policy. Sandbox only: it needs a payer that can play the vendor
        (Airwallex's issuing simulation). The answer is recorded against the decision that
        created the card, so the ledger shows what the card did on its own."""
        simulate = getattr(self.payer, "simulate_charge", None)
        if simulate is None:
            raise GatewayError(f"{self.payer.provider} cannot simulate a vendor's charge")
        with self.ledger.session() as state:
            seq = state.request_ids.get(request_id)
            decision = state.record(seq) if seq is not None else None
            if decision is None or decision["kind"] != DECISION or decision["decision"] != ALLOW:
                raise GatewayError(f"request {request_id} has no allowed decision")
            done = state.executions.get(seq, [])
        cards = _paid(done)
        if not cards:
            raise GatewayError(f"request {request_id} has no card")
        money = parse_money(amount, currency)
        result = self._call(lambda: simulate(cards[-1], money, currency, merchant_category), FORCED_CHARGE)
        return self._record(None, seq, result)

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
