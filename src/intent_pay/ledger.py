"""Every decision the engine takes, allow and refuse alike, in one append-only JSON Lines file.

One line is one record. A decision record is the receipt for one proposal: what was proposed,
what was decided, which rule decided it, and when. An execution record says what a sandbox did
with an allowed decision (a payment or a refund) and points back at that decision by number.
Each line carries the hash of the line before it, so changing or deleting an earlier line
breaks the chain from that point on and `Ledger.verify` names the line. The chain is not
signed. It shows tampering to anyone holding the file; it does not prove who wrote it.

Fail-closed, the same discipline as the daily counter in Usenami Signer's gateway: if the file
cannot be read, does not verify, or the new line cannot be written and flushed to disk, the
engine refuses. A payment is never allowed before its receipt is on disk. A half-written last
line therefore stops every later decision until someone looks at the file, by design. If the
flush fails after the bytes reached the file, the file keeps an allow the caller was told is a
refusal; it counts against the day and burns the request ID, which errs toward paying less.

The amounts a day has already allowed are read back from this file on every decision, so a
restart does not reset the day's spend. The day is the UTC calendar day.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None
    import msvcrt

VERSION = "1"
GENESIS = "0" * 64
ALLOW = "allow"
REFUSE = "refuse"
DECISION = "decision"
EXECUTION = "execution"
OPERATIONS = ("pay", "refund")

_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


class LedgerError(Exception):
    """The ledger cannot be read, does not verify, or cannot be written."""


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def iso_utc(moment: datetime) -> str:
    if moment.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class Receipt:
    """The answer to one proposal. `seq` and `hash` are None when it could not be recorded,
    and a receipt that was not recorded is always a refusal."""

    decision: str
    reason: str
    detail: str
    request_id: str | None
    intent: dict[str, Any] | None
    policy_hash: str
    at: str
    seq: int | None = None
    prev: str | None = None
    hash: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOW and self.hash is not None

    @property
    def recorded(self) -> bool:
        return self.hash is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": VERSION,
            "kind": "decision",
            "seq": self.seq,
            "at": self.at,
            "decision": self.decision,
            "reason": self.reason,
            "detail": self.detail,
            "request_id": self.request_id,
            "intent": self.intent,
            "policy_hash": self.policy_hash,
            "prev": self.prev,
            "hash": self.hash,
        }


@dataclass(frozen=True)
class Execution:
    """What a sandbox did with one allowed decision. `steps` lists each sandbox call (operation,
    HTTP status, the provider's ID and status); it never carries a key, a token or card data."""

    decision_seq: int
    provider: str
    operation: str
    ok: bool
    status: str
    reference: str | None
    detail: str
    steps: tuple[dict[str, Any], ...]
    at: str
    seq: int | None = None
    prev: str | None = None
    hash: str | None = None

    @property
    def recorded(self) -> bool:
        return self.hash is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": VERSION,
            "kind": EXECUTION,
            "seq": self.seq,
            "at": self.at,
            "decision_seq": self.decision_seq,
            "provider": self.provider,
            "operation": self.operation,
            "ok": self.ok,
            "status": self.status,
            "reference": self.reference,
            "detail": self.detail,
            "steps": [dict(step) for step in self.steps],
            "prev": self.prev,
            "hash": self.hash,
        }


@dataclass
class LedgerState:
    """What the file said when the lock was taken. Valid only inside `Ledger.session`."""

    records: list[dict[str, Any]] = field(default_factory=list)
    last_hash: str = GENESIS
    request_ids: dict[str, int] = field(default_factory=dict)
    executions: dict[int, list[dict[str, Any]]] = field(default_factory=dict)

    @property
    def next_seq(self) -> int:
        return len(self.records) + 1

    def allowed_on(self, currency: str, day: date) -> Decimal:
        """Sum of the amounts allowed in `currency` on UTC `day`. Refusals spend nothing."""
        prefix = day.isoformat()
        total = Decimal(0)
        for rec in self.records:
            intent = rec.get("intent") or {}
            if (rec["kind"] == DECISION and rec["decision"] == ALLOW and intent.get("currency") == currency
                    and rec["at"].startswith(prefix)):
                total += Decimal(intent["amount"])
        return total

    def record(self, seq: int) -> dict[str, Any] | None:
        return self.records[seq - 1] if 0 < seq <= len(self.records) else None

    def _index(self, rec: dict[str, Any]) -> None:
        self.records.append(rec)
        self.last_hash = rec["hash"]
        if rec["kind"] == DECISION and rec.get("request_id"):
            self.request_ids.setdefault(rec["request_id"], rec["seq"])
        if rec["kind"] == EXECUTION:
            self.executions.setdefault(rec["decision_seq"], []).append(rec)


def _lock(fh: Any) -> None:
    """Exclusive lock on the side file `<ledger>.lock`, held across processes."""
    if fcntl is not None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    else:  # pragma: no cover - Windows: lock byte 0; LK_LOCK retries for about 10 s, then raises
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)


def _unlock(fh: Any) -> None:
    if fcntl is None:  # pragma: no cover - Windows; flock is released by closing the file
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)


def _verify_lines(lines: list[str]) -> LedgerState:
    state = LedgerState()
    for number, line in enumerate(lines, start=1):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"line {number} is not JSON ({exc.msg})") from None
        if not isinstance(rec, dict) or rec.get("v") != VERSION or rec.get("kind") not in (DECISION, EXECUTION):
            raise LedgerError(f"line {number} is not a version {VERSION} ledger record")
        if rec.get("seq") != number:
            raise LedgerError(f"line {number} carries seq {rec.get('seq')!r}")
        if rec.get("prev") != state.last_hash:
            raise LedgerError(f"line {number} does not follow line {number - 1}")
        body = {k: v for k, v in rec.items() if k != "hash"}
        if rec.get("hash") != sha256_hex(canonical(body)):
            raise LedgerError(f"line {number} does not match its hash")
        if not isinstance(rec.get("at"), str):
            raise LedgerError(f"line {number} has no time")
        if rec["kind"] == DECISION and rec.get("decision") not in (ALLOW, REFUSE):
            raise LedgerError(f"line {number} has no valid decision")
        if rec["kind"] == EXECUTION:
            _check_execution(state, rec, number)
        state._index(rec)
    return state


def _check_execution(state: LedgerState, rec: dict[str, Any], number: int) -> None:
    """An execution must follow the allowed decision it belongs to. A payment recorded against a
    refusal, or against a line that does not exist yet, means the file was not written by us."""
    target = state.record(rec.get("decision_seq")) if isinstance(rec.get("decision_seq"), int) else None
    if target is None or target["kind"] != DECISION or target["decision"] != ALLOW:
        raise LedgerError(f"line {number} records an execution for no allowed decision")
    if rec.get("operation") not in OPERATIONS or not isinstance(rec.get("ok"), bool):
        raise LedgerError(f"line {number} has no valid operation")


class Ledger:
    """A decision ledger at `path`, or in memory when `path` is None (tests, throwaway demos)."""

    def __init__(self, path: str | os.PathLike[str] | None = None):
        self.path = pathlib.Path(path) if path is not None else None
        self._memory: list[str] = []
        key = str(self.path.resolve()) if self.path else f"memory:{id(self)}"
        with _PATH_LOCKS_GUARD:
            self._lock = _PATH_LOCKS.setdefault(key, threading.Lock())

    @contextmanager
    def session(self) -> Iterator[LedgerState]:
        """Hold the ledger, read and verify it, and yield its state. Every decision happens
        inside one session, so two proposals with the same request ID cannot both pass."""
        with self._lock:
            lock_file = None
            locked = False
            try:
                if self.path is not None:
                    try:
                        self.path.parent.mkdir(parents=True, exist_ok=True)
                        lock_file = open(self.path.with_name(self.path.name + ".lock"), "a+")
                        _lock(lock_file)
                        locked = True
                    except OSError as exc:
                        raise LedgerError(f"cannot lock the ledger: {exc}") from None
                yield _verify_lines(self._read_lines())
            finally:
                if lock_file is not None:
                    if locked:
                        try:
                            _unlock(lock_file)
                        except OSError:
                            pass  # closing the file releases it as well
                    lock_file.close()

    def append(self, state: LedgerState, *, decision: str, reason: str, detail: str,
               request_id: str | None, intent: dict[str, Any] | None, policy_hash: str,
               at: datetime) -> Receipt:
        """Write one decision receipt after the last record and return it. Raises LedgerError when
        the line could not be written and flushed; the caller then refuses."""
        draft = Receipt(decision=decision, reason=reason, detail=detail, request_id=request_id,
                        intent=intent, policy_hash=policy_hash, at=iso_utc(at),
                        seq=state.next_seq, prev=state.last_hash)
        return replace(draft, hash=self._write_record(state, draft.to_dict()))

    def append_execution(self, state: LedgerState, *, decision_seq: int, provider: str, operation: str,
                         ok: bool, status: str, reference: str | None, detail: str,
                         steps: tuple[dict[str, Any], ...], at: datetime) -> Execution:
        """Write what a sandbox did with an allowed decision. Checked the same way `verify` checks
        it, so a bad execution never reaches the file."""
        draft = Execution(decision_seq=decision_seq, provider=provider, operation=operation, ok=ok,
                          status=status, reference=reference, detail=detail, steps=tuple(steps),
                          at=iso_utc(at), seq=state.next_seq, prev=state.last_hash)
        body = draft.to_dict()
        _check_execution(state, body, state.next_seq)
        return replace(draft, hash=self._write_record(state, body))

    def _write_record(self, state: LedgerState, body: dict[str, Any]) -> str:
        body = {k: v for k, v in body.items() if k != "hash"}
        digest = sha256_hex(canonical(body))
        line = canonical({**body, "hash": digest})
        self._write_line(line)
        state._index(json.loads(line))
        return digest

    def records(self) -> list[dict[str, Any]]:
        with self.session() as state:
            return list(state.records)

    def verify(self) -> int:
        """Number of receipts, or LedgerError naming the first line that breaks the chain."""
        with self.session() as state:
            return len(state.records)

    def _read_lines(self) -> list[str]:
        if self.path is None:
            return list(self._memory)
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except (OSError, UnicodeDecodeError) as exc:
            raise LedgerError(f"cannot read the ledger: {exc}") from None
        if text and not text.endswith("\n"):
            raise LedgerError("the last line of the ledger is incomplete")
        return text.splitlines()

    def _write_line(self, line: str) -> None:
        if self.path is None:
            self._memory.append(line)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            raise LedgerError(f"cannot write the ledger: {exc}") from None
