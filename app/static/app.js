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

// --- boot ------------------------------------------------------------------
(async () => {
  try { await api("GET", "/api/session"); await loadPortal(); }
  catch (_) { showLogin(); }
})();
