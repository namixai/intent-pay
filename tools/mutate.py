"""Break the policy one rule at a time, on a copy of the repository, and run the tests.

A mutation that leaves the suite green is a rule no test defends. Run it after changing a rule
or a test; it is not part of CI because the patterns follow the code line by line.

    .venv/bin/python tools/mutate.py
"""
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
PY = pathlib.Path(sys.executable)

MUTATIONS = [
    ("per_payment inclusive -> exclusive", "src/intent_pay/policy.py",
     "if intent.amount > rule.per_payment:", "if intent.amount >= rule.per_payment:"),
    ("per_payment rule removed", "src/intent_pay/policy.py",
     "if intent.amount > rule.per_payment:", "if False:"),
    ("duplicate check removed", "src/intent_pay/policy.py",
     "if intent.request_id in state.request_ids:", "if False:"),
    ("daily ignores what was spent", "src/intent_pay/policy.py",
     "if spent + intent.amount > rule.daily:", "if intent.amount > rule.daily:"),
    ("refusals count toward the day", "src/intent_pay/ledger.py",
     'rec["kind"] == DECISION and rec["decision"] == ALLOW and intent.get("currency")', 'rec["kind"] == DECISION and intent.get("currency")'),
    ("day is not per currency", "src/intent_pay/ledger.py",
     ' and intent.get("currency") == currency\n', "\n"),
    ("day never resets", "src/intent_pay/ledger.py",
     '\n                    and rec["at"].startswith(prefix)):', "):"),
    ("payee rule removed", "src/intent_pay/policy.py",
     "if self.policy.payees is not None and intent.payee not in self.policy.payees:", "if False:"),
    ("category rule removed", "src/intent_pay/policy.py",
     "if self.policy.merchant_categories is not None and intent.merchant_category not in self.policy.merchant_categories:", "if False:"),
    ("currency rule fails open", "src/intent_pay/policy.py",
     "        if rule is None:\n", "        if rule is None and False:\n"),
    ("reserve boundary off by one", "src/intent_pay/policy.py",
     "if after < rule.min_balance:", "if after <= rule.min_balance:"),
    ("reserve fails open when balance unknown", "src/intent_pay/policy.py",
     "            if balance is None:\n                return Reason.BALANCE_UNAVAILABLE",
     "            if balance is None:\n                return Reason.OK"),
    ("garbage balance accepted", "src/intent_pay/policy.py",
     "return amount if amount.is_finite() else None", "return amount"),
    ("rule error fails open", "src/intent_pay/policy.py",
     "reason, detail = Reason.POLICY_ERROR,", "reason, detail = Reason.OK,"),
    ("write error swallowed", "src/intent_pay/ledger.py",
     'raise LedgerError(f"cannot write the ledger: {exc}") from None', "pass"),
    ("hash not checked", "src/intent_pay/ledger.py",
     'if rec.get("hash") != sha256_hex(canonical(body)):', "if False:"),
    ("chain link not checked", "src/intent_pay/ledger.py",
     'if rec.get("prev") != state.last_hash:', "if False:"),
    ("partial line accepted", "src/intent_pay/ledger.py",
     'if text and not text.endswith("\\n"):', "if False:"),
    ("no lock between threads", "src/intent_pay/ledger.py",
     "        with self._lock:\n", "        if True:\n"),
    ("no file lock between processes", "src/intent_pay/ledger.py",
     "fcntl.flock(fh.fileno(), fcntl.LOCK_EX)", "pass"),
    ("decide lets an error escape", "src/intent_pay/policy.py",
     "            return self._decide(proposal)\n        except Exception as exc:",
     "            return self._decide(proposal)\n        except ZeroDivisionError as exc:\n            raise\n        except Exception as exc:"),
    ("gateway pays a refused decision", "src/intent_pay/gateway.py",
     "        if not receipt.allowed:\n            return Outcome(receipt)", "        if False:\n            return Outcome(receipt)"),
    ("network error reads as paid", "src/intent_pay/gateway.py",
     'ok=False, status="ERROR",', 'ok=True, status="ERROR",'),
    ("refund of a failed payment", "src/intent_pay/gateway.py",
     'e["operation"] == "pay" and e["ok"] and e["reference"]', 'e["operation"] == "pay"'),
    ("refund twice", "src/intent_pay/gateway.py",
     'if any(e["operation"] == "refund" and e["ok"] for e in done):', "if False:"),
    ("execution for a refusal accepted", "src/intent_pay/ledger.py",
     'if target is None or target["kind"] != DECISION or target["decision"] != ALLOW:', "if target is None:"),
    ("order without invoice_id", "src/intent_pay/adapters/paypal.py",
     '            "invoice_id": intent.request_id,\n', ""),
    ("capture without the card", "src/intent_pay/adapters/paypal.py",
     '{"payment_source": self.config.card.payment_source()}', "{}"),
    ("live PayPal host accepted", "src/intent_pay/adapters/paypal.py",
     "parts.hostname not in SANDBOX_HOSTS", "False"),
    ("client secret in repr", "src/intent_pay/adapters/paypal.py",
     "    client_secret: str = field(repr=False)", "    client_secret: str"),
    ("no token cache", "src/intent_pay/adapters/paypal.py",
     "if token and self.clock() < self._token_expires:", "if False:"),
    ("no token margin", "src/intent_pay/adapters/paypal.py",
     "int(body.get(\"expires_in\", 0)) - TOKEN_MARGIN_S", "int(body.get(\"expires_in\", 0))"),
    ("token fetched without the lock", "src/intent_pay/adapters/paypal.py",
     "        with self._token_lock:\n            token = self._token", "        if True:\n            token = self._token"),
    ("no retry on 401", "src/intent_pay/adapters/paypal.py",
     "if resp.status != 401 or attempt == 2:", "if True:"),
    ("retry 401 without end", "src/intent_pay/adapters/paypal.py",
     "if resp.status != 401 or attempt == 2:", "if resp.status != 401:"),
    ("declined capture reads as paid", "src/intent_pay/adapters/paypal.py",
     "it can still be refunded.\n        ok = status in (\"COMPLETED\", \"PENDING\")", "it can still be refunded.\n        ok = True"),
    ("redaction off", "src/intent_pay/adapters/http.py",
     'REDACTED if k in SECRET_FIELDS and v not in (None, "") else redact(v)', "redact(v)"),
    ("authorization header recorded", "src/intent_pay/adapters/http.py",
     "if k.lower() in KEPT_HEADERS}", "if True}"),
    ("replay ignores the path", "src/intent_pay/adapters/http.py",
     "if want[\"method\"] != method or want[\"path\"].split(\"?\")[0] != path:", "if False:"),
    ("malformed does not burn its ID", "src/intent_pay/policy.py",
     "request_id = request_id if isinstance(request_id, str) and 0 < len(request_id) <= 64 else None",
     "request_id = None"),
    ("unknown fields accepted", "src/intent_pay/intent.py",
     "        if unknown:\n            raise IntentError", "        if False:\n            raise IntentError"),
    ("decimals not checked", "src/intent_pay/intent.py",
     "if amount != amount.quantize(Decimal(1).scaleb(-places)):", "if False:"),
    ("policy with no allowlist loads", "src/intent_pay/policy.py",
     "if payees is None and categories is None:", "if False:"),
    ("agent reads any page", "src/intent_pay/agent.py",
     'if args.get("url") != page.get("url"):', "if not page:"),
    ("scripted agent claims every payment", "src/intent_pay/agent.py",
     '    if payment.get("ok"):\n        what = ', '    if True:\n        what = '),
    ("run tag not put into request IDs", "src/intent_pay/agent.py",
     'return value.replace(marker, run)', "return value"),
    ("model turns unbounded", "src/intent_pay/agent.py",
     "        for _ in range(MAX_TURNS):\n            response", "        for _ in range(MAX_TURNS * 100):\n            response"),
    ("tool error reported as success", "src/intent_pay/agent.py",
     '"content": json.dumps(seen), "is_error": is_error}', '"content": json.dumps(seen), "is_error": False}'),
    ("reset keeps the old ledger", "src/intent_pay/agent.py",
     '    while (directory / f"{prefix}{tag}.jsonl").exists():', "    while False:"),
    ("page header not required", "src/intent_pay/web.py",
     'if not self._local() or self.headers.get("X-Intent-Pay") != "1":', "if not self._local():"),
    ("any host name accepted", "src/intent_pay/web.py",
     "            return host in LOCAL_HOSTS", "            return True"),
    ("card allows any merchant category", "src/intent_pay/adapters/airwallex.py",
     'controls["allowed_merchant_categories"] = sorted(policy.merchant_categories)', "pass"),
    ("card has no cap at the allowed amount", "src/intent_pay/adapters/airwallex.py",
     '                {"amount": _number(intent.amount), "interval": "ALL_TIME"},\n', ""),
    ("card per-transaction limit is the daily one", "src/intent_pay/adapters/airwallex.py",
     '{"amount": _number(rule.per_payment), "interval": "PER_TRANSACTION"}', '{"amount": _number(rule.daily), "interval": "PER_TRANSACTION"}'),
    ("card allows any currency", "src/intent_pay/adapters/airwallex.py",
     '"allowed_currencies": [intent.currency],', ""),
    ("declined authorization reads as paid", "src/intent_pay/adapters/airwallex.py",
     'if a.get("process_result") != "APPROVED" or not lifecycle:', "if not lifecycle:"),
    ("failed capture reads as paid", "src/intent_pay/adapters/airwallex.py",
     'if cap.status >= 400 or c.get("process_result") != "APPROVED":', "if cap.status >= 400:"),
    ("inactive card is charged", "src/intent_pay/adapters/airwallex.py",
     '        if status != "ACTIVE":\n', "        if False:\n"),
    ("approved vendor charge left on the card", "src/intent_pay/adapters/airwallex.py",
     "            if lifecycle:\n                rev = ", "            if False:\n                rev = "),
    ("airwallex host not checked", "src/intent_pay/adapters/airwallex.py",
     'if parts.scheme != "https" or parts.hostname not in SANDBOX_HOSTS:', 'if parts.scheme != "https":'),
    ("airwallex key sent on every call", "src/intent_pay/adapters/airwallex.py",
     'headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}',
     'headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "x-api-key": self.config.api_key}'),
    ("airwallex no retry on 401", "src/intent_pay/adapters/airwallex.py",
     "if resp.status != 401 or attempt == 2:", "if True:"),
    ("missing balance reads as plenty", "src/intent_pay/adapters/airwallex.py",
     "        return Decimal(0)\n", "        return Decimal(10 ** 9)\n"),
    ("card number recorded", "src/intent_pay/adapters/http.py",
     '"number", "card_number", "expiry"', '"number", "expiry"'),
    ("vendor charge on a refused request", "src/intent_pay/gateway.py",
     'if decision is None or decision["kind"] != DECISION or decision["decision"] != ALLOW:\n                raise GatewayError(f"request {request_id} has no allowed decision")',
     'if decision is None:\n                raise GatewayError(f"request {request_id} has no allowed decision")'),
    ("card and receipt always match", "src/intent_pay/agent.py",
     'compare["match"] = compare.get("expected") is not None and compare["card"] == compare["expected"]', 'compare["match"] = True'),
]


def run(tree: pathlib.Path) -> bool:
    out = subprocess.run([str(PY), "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=tree,
                         capture_output=True, text=True, timeout=300)
    return out.returncode == 0


def main() -> int:
    holes = 0
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp) / "repo"
        shutil.copytree(REPO, base, ignore=shutil.ignore_patterns(".venv", ".git", "*.egg-info", "__pycache__", "state"))
        assert run(base), "suite is not green before mutation"
        for name, rel, old, new in MUTATIONS:
            path = base / rel
            original = path.read_text()
            if original.count(old) != 1:
                print(f"SKIP  {name}: pattern found {original.count(old)} times")
                holes += 1
                continue
            path.write_text(original.replace(old, new))
            green = run(base)
            path.write_text(original)
            print(("HOLE  " if green else "red   ") + name)
            holes += green
    print(f"{len(MUTATIONS)} mutations, {holes} not caught")
    return 1 if holes else 0


if __name__ == "__main__":
    sys.exit(main())
