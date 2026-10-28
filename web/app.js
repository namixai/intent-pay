// The demo page. Everything shown comes from /api/state; nothing here decides anything.
"use strict";

let state = null;
let selected = 0;
let busy = false;
let seenRecords = 0;

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") el.className = value;
    else el.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
  return el;
}

const short = (hash) => (hash ? hash.slice(0, 12) : "");

async function call(method, path) {
  const options = { method, headers: method === "POST" ? { "X-Intent-Pay": "1" } : {} };
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || response.statusText);
  return body;
}

// The step's result for the cards: the last proposal decides the colour.
function stepStatus(step) {
  if (!step.events) return { text: "not run", cls: "" };
  const decisions = step.events.filter((e) => e.kind === "decision");
  if (decisions.length === 0) return { text: "no proposal", cls: "warn" };
  const last = decisions[decisions.length - 1];
  const seen = last.agent_sees;
  if (seen.decision !== "allow") return { text: `refused: ${seen.reason}`, cls: "refuse" };
  if (seen.payment && seen.payment.ok) return { text: `paid: ${seen.payment.status}`, cls: "allow" };
  return { text: `allowed, PayPal: ${seen.payment ? seen.payment.status : "no answer"}`, cls: "warn" };
}

function renderMode() {
  const mode = document.getElementById("mode");
  mode.replaceChildren(
    h("dt", null, "Agent"), h("dd", null, state.mode.agent),
    h("dt", null, "Payments"), h("dd", null, state.mode.payments),
  );
}

function renderSteps() {
  const nav = document.getElementById("steps");
  nav.replaceChildren(...state.steps.map((step, i) => {
    const status = stepStatus(step);
    const card = h("button", { type: "button", class: "step", "aria-current": i === selected ? "true" : "false" },
      h("span", { class: "n" }, `Step ${step.number}`),
      h("span", { class: "t" }, step.title),
      h("span", { class: `chip ${status.cls}` }, status.text));
    card.addEventListener("click", () => { selected = i; render(); });
    return card;
  }));
}

function pageBox(event) {
  const text = event.text || "(no such page)";
  const pre = h("pre");
  const mark = event.highlight && text.includes(event.highlight) ? event.highlight : null;
  if (mark) {
    const [before, after] = text.split(mark);
    pre.append(before, h("mark", null, mark), after);
  } else {
    pre.append(text);
  }
  return h("div", { class: "page" }, h("div", { class: "url" }, event.url || ""), pre);
}

function proposalBox(input) {
  const rows = ["amount", "currency", "billing", "merchant_category", "purpose", "request_id"]
    .filter((key) => input[key] !== undefined)
    .flatMap((key) => [h("dt", null, key), h("dd", null, input[key])]);
  return h("div", null, h("h3", null, "Proposal"), h("dl", { class: "kv" }, rows));
}

function agentLane(step) {
  const lane = h("section", { class: "lane" },
    h("div", { class: "lane-head" }, h("h2", null, "Agent"), h("span", { class: "who" }, state.mode.agent)));
  lane.append(h("p", { class: "task" }, step.task));
  for (const event of step.events || []) {
    if (event.kind === "say") lane.append(h("p", { class: "say" }, event.text));
    if (event.kind === "error") lane.append(h("p", { class: "error" }, event.text));
    if (event.kind === "read_page") lane.append(pageBox(event));
    if (event.kind === "proposal") lane.append(proposalBox(event.input));
  }
  return lane;
}

function policyLane(decisions) {
  const lane = h("section", { class: "lane" },
    h("div", { class: "lane-head" }, h("h2", null, "Policy"), h("span", { class: "who" }, "plain Python, no model")));
  if (decisions.length === 0) {
    lane.append(h("p", { class: "not-called" }, "No proposal reached the policy."));
  }
  for (const event of decisions) {
    const receipt = event.outcome.receipt || {};
    const allowed = receipt.decision === "allow";
    lane.append(
      h("div", { class: `verdict ${allowed ? "allow" : "refuse"}` }, allowed ? "ALLOW" : "REFUSE"),
      h("div", { class: "code" }, receipt.reason),
      h("p", { class: "detail" }, receipt.detail),
      h("dl", { class: "kv" },
        h("dt", null, "receipt"), h("dd", null, receipt.seq === null ? "not recorded" : `#${receipt.seq}`),
        h("dt", null, "hash"), h("dd", null, short(receipt.hash)),
        h("dt", null, "policy"), h("dd", null, short(receipt.policy_hash))));
  }
  return lane;
}

function paypalLane(decisions) {
  const lane = h("section", { class: "lane" },
    h("div", { class: "lane-head" }, h("h2", null, "PayPal"), h("span", { class: "who" }, state.mode.payments)));
  if (decisions.length === 0) {
    lane.append(h("p", { class: "not-called" }, "Nothing to send."));
  }
  for (const event of decisions) {
    const payment = event.outcome.payment;
    if (!payment) {
      lane.append(h("p", { class: "not-called" }, h("strong", null, "Not called. "),
        "The policy refused, so no request left this process."));
      continue;
    }
    lane.append(h("ul", { class: "calls" }, payment.steps.map((s) => {
      const good = ["CREATED", "COMPLETED", "PENDING"].includes(s.status);
      return h("li", null,
        h("div", { class: "call" }, `${s.call}  HTTP ${s.http}`),
        h("div", null, h("span", { class: `status ${good ? "good" : "bad"}` }, s.status || ""), s.id ? `  ${s.id}` : ""),
        s.debug_id ? h("div", { class: "call" }, `debug_id ${s.debug_id}`) : null);
    })));
    lane.append(h("p", { class: "detail" }, payment.detail));
  }
  return lane;
}

function renderDetail() {
  const step = state.steps[selected];
  const main = document.getElementById("detail");
  const decisions = (step.events || []).filter((e) => e.kind === "decision");
  const next = state.steps[selected + 1];
  const goNext = step.events && next;
  const button = h("button", { type: "button", class: "run", disabled: busy || (step.events && !next) ? true : null },
    busy ? "Running..." : goNext ? `Next: step ${next.number}` : step.events ? "All steps run" : `Run step ${step.number}`);
  button.addEventListener("click", () => {
    if (goNext) { selected += 1; render(); } else runStep(step.number);
  });
  main.replaceChildren(
    h("div", { class: "actions" }, button, h("span", { class: "who" }, step.title)),
    agentLane(step), policyLane(decisions), paypalLane(decisions));
}

function resultText(record) {
  if (record.kind === "decision") return `${record.decision}: ${record.reason}`;
  const ref = record.reference ? ` ${record.reference}` : "";
  return `${record.provider} ${record.operation}: ${record.status}${ref} (for #${record.decision_seq})`;
}

function renderLedger() {
  const { records, chain, path } = state.ledger;
  const chainEl = document.getElementById("chain");
  chainEl.className = `chain ${chain.ok ? "ok" : "bad"}`;
  chainEl.textContent = chain.ok ? `${chain.records} records, hash chain verified` : `chain broken: ${chain.error}`;
  document.getElementById("ledger").replaceChildren(...records.map((r) =>
    h("tr", { class: r.seq > seenRecords ? "new" : null },
      h("td", { class: "mono" }, r.seq),
      h("td", null, r.kind),
      h("td", { class: "mono" }, r.request_id || ""),
      h("td", null, resultText(r)),
      h("td", { class: "mono" }, (r.at || "").slice(11, 19)),
      h("td", { class: "mono" }, short(r.hash)),
      h("td", { class: "mono" }, short(r.prev)))));
  document.getElementById("ledger-path").textContent = path;
}

function render() {
  renderMode();
  renderSteps();
  renderDetail();
  renderLedger();
}

async function runStep(number) {
  busy = true;
  render();
  seenRecords = state.ledger.records.length;
  try {
    state = await call("POST", `/api/steps/${number}`);
  } catch (err) {
    alert(err.message);
  }
  busy = false;
  render();
}

document.getElementById("reset").addEventListener("click", async () => {
  state = await call("POST", "/api/reset");
  selected = 0;
  seenRecords = 0;
  render();
});

call("GET", "/api/state").then((s) => { state = s; seenRecords = s.ledger.records.length; render(); });
