// Home screen and tabs. Uses h(), api(), fmtTime(), exName(), startWizard() and loadPortal() from app.js.
"use strict";

const AUTOPILOT_STATE = {
  bought: ["Bought", "ok"], holding: ["Holding", "ok"], sold: ["Sold", "ok"], waiting: ["Waiting", "muted"],
  blocked: ["Blocked by safety check", "warn"], paused: ["Paused", "bad"], error: ["Retrying", "warn"],
};
const FEED_ICON = { buy: "▲", win: "✓", loss: "▼", blocked: "■" };
let homeTimer = null;

// --- tabs ---------------------------------------------------------------------------
function showTab(name) {
  for (const b of document.querySelectorAll("#tabs [data-tab]")) b.classList.toggle("current", b.dataset.tab === name);
  for (const p of document.querySelectorAll("[data-pane]")) p.hidden = p.dataset.pane !== name;
  try { localStorage.setItem("tab", name); } catch (_) { /* storage may be blocked */ }
}

for (const b of document.querySelectorAll("#tabs [data-tab]")) b.addEventListener("click", () => showTab(b.dataset.tab));
showTab((() => { try { return localStorage.getItem("tab") || "trade"; } catch (_) { return "trade"; } })());

// --- helpers ------------------------------------------------------------------------
const money = (x) => Number(x).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const signed = (x) => (Number(x) > 0 ? "+" : "") + money(x);
const tone = (x) => (Number(x) > 0 ? "ok" : Number(x) < 0 ? "bad" : "muted");
const price = (x) => (x == null ? "-" : Number(x).toLocaleString(undefined, { maximumSignificantDigits: 8 }));
const timeOnly = (iso) => new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });

function tile(label, value, sub, cls = "") {
  return h("div", { class: `tile ${cls}` }, h("div", { class: "tile-label" }, label), h("div", { class: "tile-value" }, value),
    sub ? h("div", { class: "tile-sub" }, sub) : null);
}

// --- home ---------------------------------------------------------------------------
async function renderHome() {
  const root = $("home");
  let data;
  try { data = await api("GET", "/api/home"); } catch (e) { root.replaceChildren(h("p", { class: "error" }, e.message)); return; }
  const accts = data.accounts.filter((a) => a.account);
  const equity = accts.reduce((n, a) => n + Number(a.account.equity), 0);
  const start = accts.reduce((n, a) => n + Number(a.account.starting_balance), 0);
  const today = accts.reduce((n, a) => n + Number(a.account.realized_pnl_today), 0);
  const open = accts.reduce((n, a) => n + a.positions.length, 0);
  const autoOn = accts.filter((a) => a.autopilot.on).length;

  const parts = [];
  if (data.kill_switch) {
    parts.push(h("div", { class: "banner bad" }, "Emergency stop is ON. No new trades will be opened. ",
      "Open trades still sell at their stop-loss or take-profit. Turn it off under Safety settings."));
  }
  parts.push(h("div", { class: "tiles" },
    tile("Mode", "Practice money", "Real-money trading is locked", "paper"),
    tile("Autopilot", autoOn ? "ON" : "OFF", autoOn ? "Buys on its own when a setup passes the safety checks" : "Nothing is bought automatically",
      autoOn ? "ok" : ""),
    tile("Account value", accts.length ? `${money(equity)} USDT` : "-", accts.length ? `Started with ${money(start)}` : null),
    tile("Total profit/loss", accts.length ? signed(equity - start) : "-", accts.length ? `Today (closed trades): ${signed(today)}` : null,
      accts.length ? tone(equity - start) : ""),
    tile("Open trades", String(open), open ? "Each sells automatically at its stop or target" : null)));

  if (!accts.length) parts.push(gettingStarted(data));
  for (const a of data.accounts) if (a.account) parts.push(accountPanel(a));
  parts.push(feedPanel(data.activity));
  root.replaceChildren(...parts);
}

function gettingStarted(data) {
  const connected = data.connected_exchanges.length > 0;
  const paper = data.accounts.length > 0;
  const step = (done, text, action) => h("li", { class: done ? "done" : "" }, h("span", {}, text), done ? null : action);
  const goExchanges = h("button", { class: "link", onclick: () => showTab("exchanges") }, "Open Exchanges");
  return h("section", { class: "panel section" },
    h("h2", {}, "Get started"),
    h("ol", { class: "steps" },
      step(connected, "Connect OKX or KuCoin with a read-and-trade API key.", goExchanges),
      step(paper, "Choose your trading pairs and turn on paper trading for that exchange.", goExchanges),
      step(false, "Create a paper account with practice money.", paper ? h("button", { class: "link", onclick: () => showTab("trade") }, "Open Trade") : null),
      step(false, "Switch on AUTO-TRADE on the Trade tab.", null)));
}

function accountPanel(a) {
  const ap = a.autopilot;
  const err = h("p", { class: "error" });
  const toggle = h("button", { class: ap.on ? "switch on" : "switch", onclick: async () => {
    const on = !ap.on;
    if (on && !confirm("Turn on Autopilot?\n\nIt will buy with practice money when the strategy finds a setup and every safety check passes. " +
      "Each trade sells automatically at its stop-loss or take-profit. Real money is never used.")) return;
    toggle.disabled = true;
    try { await api("PUT", `/api/autopilot/${a.connection_id}`, { on }); await renderHome(); }
    catch (e) { err.textContent = e.message; toggle.disabled = false; }
  } }, ap.on ? "Autopilot ON · click to turn off" : "Autopilot OFF · click to turn on");

  const pairs = ap.on
    ? (ap.pairs.length ? h("ul", { class: "pair-checks" }, ap.pairs.map((p) => {
      const [label, cls] = AUTOPILOT_STATE[p.state] || [p.state, ""];
      return h("li", {}, h("div", {}, h("strong", {}, p.symbol), " ", h("span", { class: `chip ${cls}` }, label),
        h("span", { class: "muted" }, ` · ${timeOnly(p.at)}`)), h("div", { class: "why" }, p.text));
    })) : h("p", { class: "muted" }, "Checking the pairs now…"))
    : h("p", { class: "muted" }, "Autopilot is off, so nothing will be bought automatically. You can still buy and sell yourself on the Trade tab.");

  const positions = a.positions.length ? h("table", {},
    h("thead", {}, h("tr", {}, ["Pair", "Bought at", "Price now", "Profit/loss", "Sells at a loss below", "Sells at a profit above", ""].map((x) => h("th", {}, x)))),
    h("tbody", {}, a.positions.map((p) => h("tr", {},
      h("td", {}, h("strong", {}, p.symbol)), h("td", {}, price(p.avg_price)), h("td", {}, price(p.mark) + (p.mark_is_live ? "" : " (no live price)")),
      h("td", { class: tone(p.unrealized_pnl) }, signed(p.unrealized_pnl)),
      h("td", {}, price(p.stop_price)), h("td", {}, price(p.take_profit)),
      h("td", {}, h("button", { onclick: async () => {
        if (!confirm(`Sell your practice ${p.symbol} now?`)) return;
        try { await api("POST", `/api/paper/${a.connection_id}/positions/${p.symbol}/close`); await renderHome(); } catch (e) { alert(e.message); }
      } }, "Sell now"))))))
    : h("p", { class: "muted" }, "No open trades.");

  return h("section", { class: "panel section" },
    h("div", { class: "card-head" },
      h("h2", {}, `${exName(a.exchange)} · practice account`),
      h("span", { class: `badge ${a.market === "connected" ? "confirmed" : "uncertain"}` }, a.market === "connected" ? "Live prices" : "Prices: " + a.market)),
    h("p", { class: "note" }, `Watching ${a.pairs.join(", ") || "no pairs"} on ${a.pairs.length ? "closed candles" : "-"}.` +
      (ap.on && ap.last_run_at ? ` Last check ${timeOnly(ap.last_run_at)}; checks run every 30 seconds.` : "")),
    toggle, err,
    h("h3", {}, "What Autopilot sees now"), pairs,
    h("h3", {}, "Open trades"), positions);
}

function feedPanel(items) {
  return h("section", { class: "panel section" },
    h("h2", {}, "Recent activity"),
    items.length ? h("ul", { class: "feed" }, items.map((it) => h("li", { class: `feed-${it.kind}` },
      h("span", { class: "feed-icon", "aria-hidden": "true" }, FEED_ICON[it.kind] || "•"),
      h("div", {}, h("div", {}, h("strong", {}, it.title), h("span", { class: "muted" }, ` · ${exName(it.exchange)} · ${timeOnly(it.at)}`)),
        h("div", { class: "muted" }, it.detail)))))
      : h("p", { class: "muted" }, "Nothing yet. Trades and blocked setups will appear here with the reason."));
}

function startHomeTimer() {
  if (homeTimer) clearInterval(homeTimer);
  homeTimer = setInterval(() => { if (!$("portal-view").hidden && !document.querySelector('[data-pane="home"]').hidden) renderHome(); }, 5000);
}
