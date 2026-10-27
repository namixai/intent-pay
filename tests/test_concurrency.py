"""Two proposals with the same request ID, or together over the day, must not both pass even
when they arrive at once. The reads are slowed down so a missing lock shows every time,
not once in a hundred runs."""

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from decimal import Decimal

from conftest import POLICY, intent
from intent_pay.ledger import Ledger
from intent_pay.policy import Engine, Policy

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def slow_reads(ledger, monkeypatch):
    real = ledger._read_lines

    def slow():
        lines = real()
        time.sleep(0.02)
        return lines
    monkeypatch.setattr(ledger, "_read_lines", slow)


def race(engine, proposals):
    results = []
    threads = [threading.Thread(target=lambda p=p: results.append(engine.decide(p))) for p in proposals]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_threads_share_one_lock_in_memory(clock, monkeypatch):
    # In memory there is no file lock, so only the in-process lock stands between the threads.
    ledger = Ledger()
    slow_reads(ledger, monkeypatch)
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=lambda c: Decimal(2000), clock=clock)
    results = race(engine, [intent()] * 6)
    assert sum(r.allowed for r in results) == 1


def test_threads_cannot_overspend_the_day_in_memory(clock, monkeypatch):
    ledger = Ledger()
    slow_reads(ledger, monkeypatch)
    engine = Engine(Policy.from_mapping(POLICY), ledger, balance=lambda c: Decimal(2000), clock=clock)
    results = race(engine, [intent(request_id=f"r{i}", amount="30.00") for i in range(6)])
    assert sum(r.allowed for r in results) == 3


CHILD = """
import json, sys, time
from datetime import datetime, timezone
from decimal import Decimal
from intent_pay.ledger import Ledger
from intent_pay.policy import Engine, Policy

policy, path = json.loads(sys.argv[1]), sys.argv[2]
ledger = Ledger(path)
real = ledger._read_lines
ledger._read_lines = lambda: (real(), time.sleep(0.05))[0]
engine = Engine(Policy.from_mapping(policy), ledger, balance=lambda c: Decimal(2000),
                clock=lambda: datetime(2026, 10, 27, 12, tzinfo=timezone.utc))
r = engine.decide({"request_id": "same", "amount": "49.00", "currency": "USD",
                   "payee": "billing@acme.example", "merchant_category": "5734", "purpose": "race"})
print(r.decision)
"""


def test_separate_processes_cannot_both_pass(tmp_path):
    # Separate processes share nothing but the file, so only the file lock can stop this.
    path = tmp_path / "ledger.jsonl"
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    procs = [subprocess.Popen([sys.executable, "-c", CHILD, json.dumps(POLICY), str(path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
             for _ in range(4)]
    outputs = [p.communicate(timeout=60) for p in procs]
    decisions = [out.strip() for out, err in outputs]
    assert all(p.returncode == 0 for p in procs), [err for out, err in outputs]
    assert sorted(decisions) == ["allow", "refuse", "refuse", "refuse"]
    assert Ledger(path).verify() == 4
