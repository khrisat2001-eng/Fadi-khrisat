// Exchange Connections Portal UI.
// The browser never receives API secrets: the server returns only a masked key.
// Values are rendered with textContent, never innerHTML.
"use strict";

const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (res.status === 401) { showLogin(); throw new Error("Please sign in."); }
  if (!res.ok) throw new Error((data && (data.error || data.detail)) || `Request failed (${res.status})`);
  return data;
}

const fmtTime = (iso) => (iso ? new Date(iso).toLocaleString() : "never");
const STATE_LABEL = {
  PENDING_VALIDATION: "Checking…", REJECTED: "Not connected", CONNECTED_READONLY: "Connected (read only)",
  PAPER: "Paper trading", DISCONNECTED: "Disconnected",
};
const HEALTH_LABEL = { ok: "Healthy", degraded: "Connection problem", auth_failed: "Key not working", unknown: "Unknown" };

let exchanges = [];
let wizard = null;

// --- views -----------------------------------------------------------------
function show(view) {
  for (const v of ["login-view", "portal-view", "wizard-view"]) $(v).hidden = v !== view;
}

function showLogin() { show("login-view"); }

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("POST", "/api/login", { token: $("login-token").value });
    $("login-token").value = "";
    await loadPortal();
  } catch (err) { $("login-error").textContent = err.message; }
});

$("wizard-back").addEventListener("click", () => loadPortal());

async function loadPortal() {
  exchanges = await api("GET", "/api/exchanges");
  const [conns, alerts] = await Promise.all([api("GET", "/api/connections"), api("GET", "/api/alerts")]);
  renderAlerts(alerts);
  const cards = $("cards");
  cards.replaceChildren();
  for (const ex of exchanges) {
    const mine = conns.filter((c) => c.exchange === ex.exchange_id);
    const active = mine.find((c) => c.state === "CONNECTED_READONLY" || c.state === "PAPER");
    cards.append(active ? connectionCard(ex, active) : emptyCard(ex, mine[mine.length - 1]));
  }
  await renderPaper(conns.filter((c) => c.state === "PAPER"));
  await renderRisk();
  show("portal-view");
}

function renderAlerts(alerts) {
  const box = $("alerts");
  box.replaceChildren();
  for (const a of alerts) {
    box.append(h("div", { class: `alert ${a.level}` },
      h("span", {}, `${fmtTime(a.created_at)} · ${a.message}`),
      h("button", { class: "link", onclick: async () => { await api("POST", `/api/alerts/${a.id}/ack`); loadPortal(); } }, "Dismiss")));
  }
}

function emptyCard(ex, last) {
  return h("article", { class: "card" },
    h("h2", {}, ex.display_name),
    h("p", { class: "status muted" }, "Not connected"),
    last && last.state === "REJECTED" ? issuesList(last.issues) : null,
    h("button", { onclick: () => startWizard(ex.exchange_id) }, `Connect ${ex.display_name}`));
}

function connectionCard(ex, c) {
  const msg = h("p", { class: "error" });
  const run = (fn) => async () => {
    msg.textContent = "";
    try { await fn(); await loadPortal(); } catch (err) { msg.textContent = err.message; }
  };
  const nonZero = c.balances.filter((b) => Number(b.total) !== 0);
  return h("article", { class: `card health-${c.health}` },
    h("div", { class: "card-head" },
      h("h2", {}, ex.display_name),
      h("span", { class: `mode-badge ${c.state === "PAPER" ? "paper" : "readonly"}` }, c.state === "PAPER" ? "PAPER" : "READ ONLY")),
    h("dl", {},
      h("dt", {}, "Status"), h("dd", {}, STATE_LABEL[c.state] || c.state),
      h("dt", {}, "Health"), h("dd", { class: `health ${c.health}` }, HEALTH_LABEL[c.health] || c.health),
      h("dt", {}, "API key"), h("dd", { class: "mono" }, c.key_masked),
      h("dt", {}, "Permissions"), h("dd", {}, c.raw_permissions.join(", ") || "unknown"),
      h("dt", {}, "IP restricted"), h("dd", {}, c.ip_restricted === null ? "unknown" : c.ip_restricted ? "yes" : "no"),
      h("dt", {}, "Last sync"), h("dd", {}, fmtTime(c.last_sync_at)),
      h("dt", {}, "Last check"), h("dd", {}, fmtTime(c.last_test_at)),
      h("dt", {}, "Allocation"), h("dd", {}, c.allocation_pct ? `${c.allocation_pct}%` : "not set"),
      h("dt", {}, "Pairs"), h("dd", {}, c.selected_pairs.length ? c.selected_pairs.join(", ") : "none selected")),
    c.last_error ? h("p", { class: "error" }, c.last_error) : null,
    issuesList(c.issues),
    h("details", {}, h("summary", {}, `Balances (${nonZero.length})`), balancesTable(nonZero)),
    h("div", { class: "actions" },
      h("button", { onclick: run(() => api("POST", `/api/connections/${c.id}/test`)) }, "Test connection"),
      h("button", { onclick: run(() => api("POST", `/api/connections/${c.id}/sync`)) }, "Sync balances"),
      h("button", { onclick: () => startWizard(ex.exchange_id, c, 8) }, "Allocation & pairs"),
      c.state === "CONNECTED_READONLY"
        ? h("button", { onclick: () => startWizard(ex.exchange_id, c, 9) }, "Enable paper trading") : null,
      h("button", { onclick: () => replaceKey(ex, c) }, "Replace key"),
      h("button", { class: "danger", onclick: run(async () => {
        if (!confirm(`Disconnect ${ex.display_name}? The stored key will be deleted from our server.`)) return;
        await api("DELETE", `/api/connections/${c.id}`);
      }) }, "Disconnect")),
    msg);
}

function issuesList(issues) {
  if (!issues || !issues.length) return null;
  return h("ul", { class: "issues" }, issues.map((i) => h("li", { class: i.level }, i.message)));
}

function balancesTable(rows) {
  if (!rows.length) return h("p", { class: "muted" }, "No balances.");
  return h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Asset"), h("th", {}, "Total"), h("th", {}, "Available"))),
    h("tbody", {}, rows.map((b) => h("tr", {}, h("td", {}, b.currency), h("td", {}, b.total), h("td", {}, b.available)))));
}

function credentialForm(ex, submitLabel, onSubmit) {
  const inputs = {};
  const err = h("p", { class: "error" });
  const btn = h("button", { type: "submit" }, submitLabel);
  const form = h("form", { autocomplete: "off", onsubmit: async (e) => {
      e.preventDefault();
      err.textContent = "";
      btn.disabled = true;
      const body = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
      try { await onSubmit(body); }
      catch (e2) { err.textContent = e2.message; }
      finally {
        for (const el of Object.values(inputs)) el.value = ""; // don't keep secrets in the page
        btn.disabled = false;
      }
    } },
    ex.credential_fields.map((f) => {
      inputs[f.name] = h("input", { type: "password", name: f.name, required: true, autocomplete: "new-password", spellcheck: "false", maxlength: "256" });
      return h("label", {}, h("span", {}, f.label), inputs[f.name], h("small", {}, f.help));
    }),
    h("p", { class: "note" }, "Your key is sent only to this app's server over HTTPS, encrypted there, and never shown again in full. Never paste API keys into chat, email, source code or links."),
    btn, err);
  return form;
}

function replaceKey(ex, c) {
  wizard = { exchange: ex, connection: c, step: 4 };
  show("wizard-view");
  renderStepper();
  $("wizard-body").replaceChildren(
    h("h2", {}, `Replace ${ex.display_name} key`),
    h("p", {}, "We'll test the new key first. The old key is only replaced if the new one works and has safe permissions."),
    credentialForm(ex, "Test and replace", async (body) => {
      await api("PUT", `/api/connections/${c.id}/credentials`, body);
      await loadPortal();
    }));
}

// --- wizard ----------------------------------------------------------------
function startWizard(exchangeId, connection = null, step = 1) {
  const ex = exchanges.find((e) => e.exchange_id === exchangeId);
  wizard = { exchange: ex, connection, step: exchangeId && step === 1 ? 2 : step };
  show("wizard-view");
  renderWizard();
}

function go(step) { wizard.step = step; renderWizard(); }

function renderStepper() {
  const labels = [...$("tpl-step-labels").content.querySelectorAll("li")].map((li) => li.textContent);
  $("stepper").replaceChildren(...labels.map((l, i) =>
    h("li", { class: i + 1 === wizard.step ? "current" : i + 1 < wizard.step ? "done" : "" }, l)));
}

function renderWizard() {
  renderStepper();
  const ex = wizard.exchange;
  const body = $("wizard-body");
  const next = (n, label = "Next") => h("button", { onclick: () => go(n) }, label);
  switch (wizard.step) {
    case 1:
      body.replaceChildren(h("h2", {}, "Which exchange?"),
        h("div", { class: "choices" }, exchanges.map((e) =>
          h("button", { class: "choice", onclick: () => { wizard.exchange = e; go(2); } }, e.display_name))));
      break;
    case 2:
      body.replaceChildren(h("h2", {}, `Create an API key on ${ex.display_name}`),
        h("ol", {}, ex.create_key_steps.map((s) => h("li", {}, s))),
        h("p", {}, h("a", { href: ex.api_key_page_url, target: "_blank", rel: "noopener noreferrer" }, `Open ${ex.display_name} API settings`)),
        next(3));
      break;
    case 3:
      body.replaceChildren(h("h2", {}, "Permissions"),
        h("p", {}, "Turn on only: ", h("strong", {}, ex.permissions_to_enable.join(", "))),
        h("p", { class: "warn" }, "Never turn on: ", h("strong", {}, ex.permissions_to_never_enable.join(", ")),
          ". We reject keys that can withdraw or transfer funds."),
        h("h3", {}, "IP restriction (recommended)"),
        h("p", {}, ex.ip_restriction_note),
        ex.server_ips.length
          ? h("p", {}, "Our server IP addresses: ", h("code", {}, ex.server_ips.join(", ")))
          : h("p", { class: "muted" }, "Server IP addresses haven't been configured yet (SERVER_EGRESS_IPS)."),
        h("ul", {}, ex.notes.map((n) => h("li", {}, n))),
        next(4));
      break;
    case 4:
      body.replaceChildren(h("h2", {}, `Enter your ${ex.display_name} API key`),
        credentialForm(ex, "Connect and test", async (creds) => {
          wizard.step = 5; renderStepper();
          body.replaceChildren(h("p", { class: "spinner" }, "Testing your key with a read-only request…"));
          try {
            wizard.connection = await api("POST", "/api/connections", { exchange: ex.exchange_id, ...creds });
          } catch (e) { go(4); throw e; }
          go(wizard.connection.state === "REJECTED" ? 7 : 6);
        }));
      break;
    case 6: {
      const c = wizard.connection;
      body.replaceChildren(h("h2", {}, "Your balances"),
        balancesTable(c.balances.filter((b) => Number(b.total) !== 0)),
        h("p", { class: "muted" }, `${c.available_pairs.length} spot pairs available for trading.`),
        next(7));
      break;
    }
    case 7: {
      const c = wizard.connection;
      const ok = c.state !== "REJECTED";
      body.replaceChildren(h("h2", {}, ok ? "Connected" : "We couldn't connect this key"),
        h("p", {}, ok ? `Key ${c.key_masked} works. Permissions: ${c.raw_permissions.join(", ") || "unknown"}.`
                      : "Nothing was saved. Your key has been deleted from our server."),
        issuesList(c.issues) || h("p", {}, "No problems found."),
        ok ? next(8) : h("button", { onclick: () => go(4) }, "Try another key"));
      break;
    }
    case 8: renderAllocation(body); break;
    case 9:
      body.replaceChildren(h("h2", {}, "Enable market data and paper trading"),
        h("p", {}, "Paper trading uses real prices from the exchange but simulated orders. No real orders are placed and no funds move."),
        h("button", { onclick: async () => {
          try { wizard.connection = await api("POST", `/api/connections/${wizard.connection.id}/paper`); go(10); }
          catch (e) { body.append(h("p", { class: "error" }, e.message)); }
        } }, "Enable paper trading"));
      break;
    case 10:
      body.replaceChildren(h("h2", {}, "Live trading is locked"),
        h("p", {}, "Live automated trading stays off. It can be unlocked only after all of these pass, and only with your explicit authorization:"),
        h("ul", {}, ["Exchange connections", "Technical strategy engine", "News monitoring", "Risk controls", "Paper trading results"].map((x) => h("li", {}, x))),
        h("button", { onclick: () => loadPortal() }, "Finish"));
      break;
  }
}

function renderAllocation(body) {
  const c = wizard.connection;
  const selected = new Set(c.selected_pairs);
  const alloc = h("input", { type: "number", min: "1", max: "100", step: "1", value: c.allocation_pct || 10 });
  const filter = h("input", { type: "search", placeholder: "Search pairs, e.g. BTC" });
  const list = h("div", { class: "pairs" });
  const chosen = h("p", { class: "muted" });
  const err = h("p", { class: "error" });
  const draw = () => {
    const q = filter.value.trim().toUpperCase();
    const shown = c.available_pairs.filter((p) => !q || p.includes(q)).slice(0, 60);
    list.replaceChildren(...shown.map((p) => {
      const cb = h("input", { type: "checkbox", checked: selected.has(p), onchange: (e) => { e.target.checked ? selected.add(p) : selected.delete(p); draw(); } });
      return h("label", { class: "pair" }, cb, p);
    }));
    chosen.textContent = selected.size ? `Selected: ${[...selected].join(", ")}` : "No pairs selected yet.";
  };
  filter.addEventListener("input", draw);
  draw();
  body.replaceChildren(h("h2", {}, "Allocation and trading pairs"),
    h("label", {}, h("span", {}, "Maximum share of this account's balance the app may use (%)"), alloc),
    h("label", {}, h("span", {}, "Trading pairs"), filter), list, chosen,
    h("button", { onclick: async () => {
      err.textContent = "";
      try {
        wizard.connection = await api("PUT", `/api/connections/${c.id}/settings`, { allocation_pct: Number(alloc.value), pairs: [...selected] });
        if (wizard.connection.state === "PAPER") loadPortal(); else go(9);
      } catch (e) { err.textContent = e.message; }
    } }, "Save"), err);
}

// --- paper trading ---------------------------------------------------------
let liveTimer = null;
let decisionLoaders = {};
document.addEventListener("decided", (e) => { const load = decisionLoaders[e.detail]; if (load) load(); });
const exName = (id) => (exchanges.find((e) => e.exchange_id === id) || {}).display_name || id;

async function renderPaper(paperConns) {
  const root = $("paper");
  root.replaceChildren();
  decisionLoaders = {};
  if (liveTimer) clearInterval(liveTimer);
  if (!paperConns.length) return;
  const liveBoxes = [];
  for (const c of paperConns) {
    const live = h("div");
    liveBoxes.push([c, live]);
    root.append(h("section", { class: "panel section" },
      h("h2", {}, `${exName(c.exchange)} paper trading`),
      live,
      signalsBox(c),
      orderForm(c),
      decisionsBox(c)));
  }
  const refresh = async () => {
    if ($("portal-view").hidden) return;
    try {
      const market = await api("GET", "/api/market");
      for (const [c, box] of liveBoxes) box.replaceChildren(await liveView(c, market));
    } catch (_) { /* next tick retries */ }
  };
  await refresh();
  liveTimer = setInterval(refresh, 3000);
}

async function liveView(c, market) {
  const view = await api("GET", `/api/paper/${c.id}`);
  const stream = market.streams.find((s) => s.exchange === c.exchange);
  const prices = market.tickers.filter((t) => t.exchange === c.exchange && c.selected_pairs.includes(t.symbol));
  const parts = [
    h("p", { class: "muted" }, `Market data: ${stream ? stream.state : "stopped"}` +
      (stream && stream.reconnects ? ` · ${stream.reconnects} reconnects` : "")),
    prices.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Pair", "Bid", "Ask", "Spread (bps)", "Age (s)"].map((x) => h("th", {}, x)))),
      h("tbody", {}, prices.map((t) => h("tr", {}, [t.symbol, t.bid, t.ask, t.spread_bps, t.age_seconds].map((x) => h("td", {}, x))))))
      : h("p", { class: "muted" }, "Waiting for prices…"),
  ];
  if (!view.account) {
    const bal = h("input", { type: "number", min: "1", step: "any", placeholder: "Leave empty to use your allocation of USDT" });
    const err = h("p", { class: "error" });
    parts.push(h("div", {}, h("h3", {}, "Create paper account"),
      h("label", {}, h("span", {}, "Starting paper balance (USDT)"), bal),
      h("button", { onclick: async () => {
        try { await api("POST", `/api/paper/${c.id}/account`, bal.value ? { starting_balance: bal.value } : {}); loadPortal(); }
        catch (e) { err.textContent = e.message; }
      } }, "Create account"), err));
    return h("div", {}, parts);
  }
  const a = view.account;
  parts.push(h("dl", {},
    h("dt", {}, "Equity"), h("dd", {}, `${a.equity} ${a.currency}`),
    h("dt", {}, "Cash"), h("dd", {}, `${a.cash} ${a.currency}`),
    h("dt", {}, "Total P&L"), h("dd", {}, `${a.total_pnl} (start ${a.starting_balance})`),
    h("dt", {}, "Realized today"), h("dd", {}, a.realized_pnl_today)));
  parts.push(h("h3", {}, "Open positions"));
  parts.push(view.positions.length ? h("table", {},
    h("thead", {}, h("tr", {}, ["Pair", "Qty", "Avg", "Mark", "Stop", "Target", "Unrealized", ""].map((x) => h("th", {}, x)))),
    h("tbody", {}, view.positions.map((p) => h("tr", {},
      [p.symbol, p.qty, p.avg_price, p.mark, p.stop_price, p.take_profit || "-", p.unrealized_pnl].map((x) => h("td", {}, x)),
      h("td", {},
        h("button", { onclick: async () => {
          const v = prompt(`New stop for ${p.symbol} (can only move up from ${p.stop_price})`);
          if (!v) return;
          try { await api("PUT", `/api/paper/${c.id}/positions/${p.symbol}/stop`, { stop_price: v }); } catch (e) { alert(e.message); }
        } }, "Tighten stop"),
        h("button", { onclick: async () => {
          if (!confirm(`Close the paper position in ${p.symbol}?`)) return;
          try { await api("POST", `/api/paper/${c.id}/positions/${p.symbol}/close`); } catch (e) { alert(e.message); }
        } }, "Close"))))))
    : h("p", { class: "muted" }, "No open positions."));
  return h("div", {}, parts);
}

function signalsBox(c) {
  const box = h("div");
  const out = h("div");
  const load = async () => {
    try {
      const sigs = await api("GET", `/api/strategy/${c.id}/signals`);
      box.replaceChildren(h("table", {},
        h("thead", {}, h("tr", {}, ["Pair", "Signal", "Close", "RSI", "ATR", "Support", "Resistance", "Stop / target", ""].map((x) => h("th", {}, x)))),
        h("tbody", {}, sigs.map((sg) => {
          const m = sg.metrics || {};
          return h("tr", {},
            h("td", {}, sg.symbol),
            h("td", { class: `sig ${sg.status.toLowerCase()}`, title: sg.reasons.join("\n") }, sg.status.replace("_", " ")),
            ...[m.close, m.rsi, m.atr, m.support, m.resistance].map((x) => h("td", {}, x ?? "-")),
            h("td", {}, sg.suggested_stop ? `${sg.suggested_stop} / ${sg.suggested_target}` : "-"),
            h("td", {}, sg.status === "BUY_SETUP" ? h("button", { onclick: async () => {
              out.replaceChildren();
              try {
                const r = await api("POST", `/api/paper/${c.id}/orders`, { symbol: sg.symbol, source: "strategy" });
                out.append(decisionView(r.decision, r.fill));
                document.dispatchEvent(new CustomEvent("decided", { detail: c.id }));
              } catch (e) { out.append(h("p", { class: "error" }, e.message)); }
            } }, "Buy with strategy") : null));
        }))),
        h("details", {}, h("summary", {}, "Why?"), sigs.map((sg) =>
          h("div", {}, h("strong", {}, sg.symbol), h("ul", { class: "checks" }, sg.reasons.map((r) => h("li", {}, r)))))));
    } catch (e) { box.replaceChildren(h("p", { class: "error" }, e.message)); }
  };
  load();
  const timer = setInterval(() => { if (!document.body.contains(box)) clearInterval(timer); else load(); }, 15000);
  return h("div", {}, h("h3", {}, "Strategy signals (trend breakout)"),
    h("p", { class: "note" }, "Based on closed candles. A signal is only a suggestion: every order still goes through the decision gate."),
    h("button", { class: "link", onclick: load }, "Refresh"), box, out);
}

function orderForm(c) {
  const sym = h("select", {}, c.selected_pairs.map((p) => h("option", { value: p }, p)));
  const stop = h("input", { type: "number", step: "any", min: "0", required: true });
  const tp = h("input", { type: "number", step: "any", min: "0", required: true });
  const out = h("div");
  return h("form", { class: "order", onsubmit: async (e) => {
      e.preventDefault();
      out.replaceChildren();
      try {
        const r = await api("POST", `/api/paper/${c.id}/orders`, { symbol: sym.value, stop_price: stop.value, take_profit_price: tp.value });
        out.append(decisionView(r.decision, r.fill));
        document.dispatchEvent(new CustomEvent("decided", { detail: c.id }));
      } catch (err) { out.append(h("p", { class: "error" }, err.message)); }
    } },
    h("h3", {}, "New paper buy"),
    h("p", { class: "note" }, "Size is calculated for you from your risk settings. Every order goes through the decision gate."),
    h("div", { class: "row" },
      h("label", {}, h("span", {}, "Pair"), sym),
      h("label", {}, h("span", {}, "Stop-loss"), stop),
      h("label", {}, h("span", {}, "Take-profit"), tp)),
    h("button", { type: "submit" }, "Check and buy (paper)"),
    out);
}

function decisionView(d, fill) {
  return h("div", { class: `decision ${d.status.toLowerCase()}` },
    h("p", {}, h("strong", {}, d.status), " · ", d.summary),
    fill ? h("p", {}, `Filled ${fill.qty} at ${Number(fill.price).toFixed(6)} (fee ${Number(fill.fee).toFixed(4)})`) : null,
    h("ul", { class: "checks" }, d.checks.map((ch) => h("li", { class: ch.result }, `${ch.label}: ${ch.detail}`))));
}

function decisionsBox(c) {
  const box = h("div");
  const load = async () => {
    const list = await api("GET", `/api/decisions?connection_id=${c.id}`);
    box.replaceChildren(...list.slice(0, 20).map((d) => h("details", {},
      h("summary", {}, `${fmtTime(d.at)} · ${d.symbol} · ${d.status} · ${d.summary}`),
      h("ul", { class: "checks" }, d.checks.map((ch) => h("li", { class: ch.result }, `${ch.label}: ${ch.detail}`))))));
    if (!list.length) box.append(h("p", { class: "muted" }, "No decisions yet."));
  };
  load();
  decisionLoaders[c.id] = load;
  return h("div", {}, h("h3", {}, "Decision log"), h("button", { class: "link", onclick: load }, "Refresh"), box);
}

// --- risk controls ---------------------------------------------------------
async function renderRisk() {
  const [cfg, ks, susp, scfg] = await Promise.all([api("GET", "/api/risk/config"), api("GET", "/api/risk/kill-switch"),
    api("GET", "/api/risk/suspensions"), api("GET", "/api/strategy/config")]);
  const sInputs = {};
  const sLabels = {
    timeframe: "Timeframe (5m, 15m, 1h, 4h)", breakout_lookback: "Support/resistance lookback (candles)", ema_fast: "Fast EMA",
    ema_slow: "Slow EMA", rsi_min: "Min RSI", rsi_max: "Max RSI (overheated above)", volume_multiple: "Breakout volume × average",
    stop_atr: "Suggested stop (ATR)", target_atr: "Suggested target (ATR)", max_breakout_extension_atr: "Max distance above breakout (ATR)",
    max_ema_extension_atr: "Max distance above fast EMA (ATR)",
  };
  const err = h("p", { class: "error" });
  const inputs = {};
  const labels = {
    max_risk_per_trade_pct: "Max risk per trade (% of equity)", max_position_pct: "Max position size (% of equity)",
    max_open_positions: "Max open positions", daily_loss_limit_pct: "Daily loss limit (%)", min_reward_risk: "Min net reward:risk",
    max_spread_bps: "Max spread (bps)", max_data_age_seconds: "Max price age (s)", taker_fee_rate: "Taker fee (0.001 = 0.1%)",
    slippage_bps: "Assumed slippage (bps)", min_stop_distance_bps: "Min stop distance (bps)", min_order_value: "Min order value (USDT)",
  };
  const target = h("input", { type: "text", placeholder: "BTC or okx" });
  const scope = h("select", {}, ["asset", "exchange", "global"].map((s) => h("option", { value: s }, s)));
  const reason = h("input", { type: "text", placeholder: "Reason" });
  $("risk").replaceChildren(h("section", { class: "panel section" },
    h("h2", {}, "Risk controls"),
    h("div", { class: `killswitch ${ks.on ? "on" : ""}` },
      h("strong", {}, ks.on ? "Emergency stop is ON: no new entries." : "Emergency stop is off."),
      h("button", { class: ks.on ? "" : "danger", onclick: async () => {
        if (!ks.on && !confirm("Turn on the emergency stop? New entries are blocked; open positions keep their stops.")) return;
        await api("POST", "/api/risk/kill-switch", { on: !ks.on, reason: "manual" }); renderRisk();
      } }, ks.on ? "Turn off" : "Emergency stop")),
    h("h3", {}, "Trading suspensions"),
    susp.length ? h("ul", {}, susp.map((s) => h("li", {}, `${s.scope} ${s.target}: ${s.reason} `,
      h("button", { class: "link", onclick: async () => { await api("DELETE", `/api/risk/suspensions/${s.id}`); renderRisk(); } }, "Lift"))))
      : h("p", { class: "muted" }, "None active."),
    h("div", { class: "row" }, scope, target, reason,
      h("button", { onclick: async () => {
        try { await api("POST", "/api/risk/suspensions", { scope: scope.value, target: target.value || "*", reason: reason.value }); renderRisk(); }
        catch (e) { err.textContent = e.message; }
      } }, "Suspend")),
    h("details", {}, h("summary", {}, "Risk settings"),
      h("div", { class: "grid2" }, Object.keys(labels).map((k) => {
        inputs[k] = h("input", { type: "number", step: "any", value: cfg[k] });
        return h("label", {}, h("span", {}, labels[k]), inputs[k]);
      })),
      h("button", { onclick: async () => {
        err.textContent = "";
        const body = Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, el.value]));
        try { await api("PUT", "/api/risk/config", body); renderRisk(); } catch (e) { err.textContent = e.message; }
      } }, "Save risk settings")),
    h("details", {}, h("summary", {}, "Strategy settings"),
      h("div", { class: "grid2" }, Object.keys(sLabels).map((k) => {
        sInputs[k] = h("input", { type: k === "timeframe" ? "text" : "number", step: "any", value: scfg[k] });
        return h("label", {}, h("span", {}, sLabels[k]), sInputs[k]);
      })),
      h("button", { onclick: async () => {
        err.textContent = "";
        const body = { ...scfg, ...Object.fromEntries(Object.entries(sInputs).map(([k, el]) => [k, k === "timeframe" ? el.value : Number(el.value)])) };
        try { await api("PUT", "/api/strategy/config", body); renderRisk(); } catch (e) { err.textContent = e.message; }
      } }, "Save strategy settings")),
    err));
}

// --- boot ------------------------------------------------------------------
(async () => {
  try { await api("GET", "/api/session"); await loadPortal(); }
  catch (_) { showLogin(); }
})();
