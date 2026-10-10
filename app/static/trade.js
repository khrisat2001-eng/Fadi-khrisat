// Exchange-style Trade screen: ranked market list, candlestick chart with the strategy's lines,
// buy/sell panel and one Auto-trade switch. Uses h(), api(), exName() and showTab() from app.js/home.js.
// Everything is practice money: the server refuses real-money orders.
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const trade = { cid: null, conns: [], symbol: null, side: "buy", markets: null, chart: null, account: null, timer: null, result: null };

function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) el.setAttribute(k, v);
  for (const c of children.flat()) if (c !== null && c !== undefined) el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return el;
}

const fmtP = (x) => (x == null ? "-" : Number(x).toLocaleString(undefined, { maximumSignificantDigits: 7 }));
const pct = (x) => (x == null ? "-" : `${x > 0 ? "+" : ""}${x.toFixed(2)}%`);
const updown = (x) => (x == null ? "" : x >= 0 ? "up" : "down");
const base = (sym) => (sym || "").split("-")[0];

// --- data -------------------------------------------------------------------------
async function loadTrade(full = true) {
  const root = $("trade");
  if (!root) return;
  try {
    if (full || !trade.conns.length) {
      trade.conns = (await api("GET", "/api/connections")).filter((c) => c.state === "PAPER");
      if (!trade.conns.find((c) => c.id === trade.cid)) trade.cid = trade.conns.length ? trade.conns[0].id : null;
    }
    if (!trade.cid) { renderTradeEmpty(root); return; }
    const [markets, account, auto] = await Promise.all([
      api("GET", `/api/markets/${trade.cid}`), api("GET", `/api/paper/${trade.cid}`), api("GET", `/api/autopilot/${trade.cid}`)]);
    trade.markets = markets; trade.account = account; trade.auto = auto;
    if (!markets.markets.find((m) => m.symbol === trade.symbol)) trade.symbol = markets.best || (markets.markets[0] || {}).symbol || null;
    trade.chart = trade.symbol ? await api("GET", `/api/chart/${trade.cid}/${encodeURIComponent(trade.symbol)}`) : null;
    renderTrade(root);
  } catch (e) { root.replaceChildren(h("p", { class: "error" }, e.message)); }
}

function startTradeTimer() {
  if (trade.timer) clearInterval(trade.timer);
  trade.timer = setInterval(() => {
    const pane = document.querySelector('[data-pane="trade"]');
    if (!$("portal-view").hidden && pane && !pane.hidden && !trade.editing) loadTrade(false);
  }, 5000);
}

function renderTradeEmpty(root) {
  root.replaceChildren(h("div", { class: "panel" },
    h("h2", {}, "Connect an exchange to start trading"),
    h("p", {}, "Connect OKX or KuCoin, choose your trading pairs and switch on paper trading. Then this screen shows the market, " +
      "the chart and the buy and sell buttons."),
    h("button", { class: "buy", onclick: () => showTab("exchanges") }, "Open Exchanges")));
}

// --- layout -----------------------------------------------------------------------
function renderTrade(root) {
  const m = trade.markets.markets.find((x) => x.symbol === trade.symbol) || {};
  const acct = trade.account.account;
  const conn = trade.conns.find((c) => c.id === trade.cid);
  root.replaceChildren(h("div", { class: "trade" },
    h("div", { class: "t-bar" },
      trade.conns.length > 1
        ? h("select", { onchange: (e) => { trade.cid = e.target.value; trade.symbol = null; loadTrade(); } },
          trade.conns.map((c) => h("option", { value: c.id, selected: c.id === trade.cid }, exName(c.exchange))))
        : h("span", { class: "t-ex" }, exName(conn.exchange)),
      h("div", { class: "t-pair" }, h("strong", {}, trade.symbol || "-"),
        h("span", { class: `t-price ${updown(m.change_24h_pct)}` }, fmtP(m.last)),
        h("span", { class: `t-chg ${updown(m.change_24h_pct)}` }, `24h ${pct(m.change_24h_pct)}`)),
      acct ? h("div", { class: "t-stat" }, h("small", {}, "Practice balance"), h("span", {}, `${money(acct.equity)} USDT`)) : null,
      acct ? h("div", { class: "t-stat" }, h("small", {}, "Profit/loss"),
        h("span", { class: updown(Number(acct.total_pnl)) }, signed(acct.total_pnl))) : null,
      autoSwitch()),
    h("div", { class: "t-grid" }, marketList(), chartPanel(), orderPanel()),
    bottomPanel()));
}

function autoSwitch() {
  const on = trade.auto && trade.auto.on;
  const err = h("span", { class: "error" });
  const btn = h("button", { class: `t-auto ${on ? "on" : ""}`, title: "Automatic buying and selling with practice money", onclick: async () => {
    if (!on && !confirm("Turn on AUTO-TRADE?\n\nThe app will study the charts of your pairs every 30 seconds and:\n" +
      "• BUY when the strategy finds a setup and every safety check passes\n" +
      "• SELL at the stop-loss, at the take-profit, or when the trend weakens\n" +
      "• raise the stop-loss as the price rises\n\nPractice money only. Real money is never used.")) return;
    btn.disabled = true;
    try { await api("PUT", `/api/autopilot/${trade.cid}`, { on: !on }); await loadTrade(false); }
    catch (e) { err.textContent = e.message; btn.disabled = false; }
  } }, h("span", { class: "dot" }), on ? "AUTO-TRADE ON" : "AUTO-TRADE OFF");
  return h("div", { class: "t-autobox" }, btn, err);
}

function marketList() {
  const best = trade.markets.best;
  return h("div", { class: "t-markets t-card" },
    h("div", { class: "t-head" }, h("span", {}, "Markets"), h("small", {}, "ranked by chance")),
    h("div", { class: "t-mrow t-mhead" }, h("span", {}, "Pair"), h("span", {}, "Price"), h("span", {}, "Chance")),
    trade.markets.markets.map((m) => h("button", {
      class: `t-mrow ${m.symbol === trade.symbol ? "sel" : ""}`,
      onclick: () => { trade.symbol = m.symbol; trade.result = null; trade.editing = false; loadTrade(false); },
    },
    h("span", {}, h("strong", {}, base(m.symbol)), h("small", {}, "/USDT"),
      m.symbol === best ? h("em", { class: "tag up" }, "BEST") : null, m.holding ? h("em", { class: "tag" }, "HOLD") : null),
    h("span", { class: "num" }, fmtP(m.last), h("small", { class: updown(m.change_24h_pct) }, pct(m.change_24h_pct))),
    h("span", { class: "score" }, h("i", { style: null, class: `bar s${Math.round(m.score / 20)}` }), `${m.score}`))),
    h("p", { class: "t-note" }, "Chance = how many of the strategy's 5 chart conditions are met right now (20 points each)."));
}

// --- chart ------------------------------------------------------------------------
function chartPanel() {
  const c = trade.chart;
  const info = h("div", { class: "t-ohlc" });
  const box = h("div", { class: "t-chart t-card" },
    h("div", { class: "t-head" }, h("span", {}, `${trade.symbol} · ${c ? c.timeframe : ""} candles`),
      h("small", { class: "legend" }, h("i", { class: "l-fast" }), `EMA${c ? c.ema_fast_period : ""} `, h("i", { class: "l-slow" }),
        `EMA${c ? c.ema_slow_period : ""} `, h("i", { class: "l-res" }), "breakout level")),
    info);
  if (!c || !c.candles.length) { box.append(h("p", { class: "muted" }, "No chart data yet.")); return box; }
  box.append(drawChart(c, info));
  return box;
}

function drawChart(c, info) {
  const narrow = window.innerWidth < 720;  // fewer, larger units on phones so labels stay readable
  const W = narrow ? 420 : 800, H = narrow ? 300 : 380, padR = 64, padB = 22, volH = narrow ? 36 : 50;
  const n = c.candles.length, step = (W - padR) / n, bw = Math.max(1, step * 0.65);
  const pos = c.position;
  const vals = c.candles.flatMap((k) => [k[2], k[3]]).concat([c.resistance, pos && pos.stop, pos && pos.target, c.last].filter((x) => x != null));
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const padV = (hi - lo) * 0.06 || 1; lo -= padV; hi += padV;
  const ph = H - padB - volH - 6;
  const y = (v) => 6 + (hi - v) / (hi - lo) * ph;
  const x = (i) => i * step + step / 2;
  const maxVol = Math.max(...c.candles.map((k) => k[5])) || 1;
  const g = s("svg", { viewBox: `0 0 ${W} ${H}`, class: "t-svg", role: "img", "aria-label": `${c.symbol} candlestick chart` });

  for (let i = 0; i <= 4; i++) {  // grid and price axis
    const v = lo + (hi - lo) * i / 4;
    g.append(s("line", { x1: 0, x2: W - padR, y1: y(v), y2: y(v), class: "grid" }), s("text", { x: W - padR + 6, y: y(v) + 4, class: "axis" }, fmtP(v)));
  }
  for (let i = 0; i < n; i += Math.ceil(n / (narrow ? 3 : 6))) {
    const d = new Date(c.candles[i][0]);
    g.append(s("text", { x: x(i), y: H - 6, class: "axis", "text-anchor": "middle" },
      d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })));
  }
  c.candles.forEach((k, i) => {  // volume and candles
    const up = k[4] >= k[1];
    const vh = k[5] / maxVol * volH;
    g.append(s("rect", { x: x(i) - bw / 2, y: H - padB - vh, width: bw, height: vh, class: `vol ${up ? "up" : "down"}` }));
    g.append(s("line", { x1: x(i), x2: x(i), y1: y(k[2]), y2: y(k[3]), class: `wick ${up ? "up" : "down"}` }));
    g.append(s("rect", { x: x(i) - bw / 2, y: y(Math.max(k[1], k[4])), width: bw, height: Math.max(1, Math.abs(y(k[1]) - y(k[4]))),
      class: `body ${up ? "up" : "down"}` }));
  });
  const line = (arr, cls) => {
    const pts = arr.map((v, i) => (v == null ? null : `${x(i).toFixed(1)},${y(v).toFixed(1)}`)).filter(Boolean).join(" ");
    if (pts) g.append(s("polyline", { points: pts, class: cls }));
  };
  line(c.ema_slow, "ema slow"); line(c.ema_fast, "ema fast");
  const level = (v, cls, label) => {
    if (v == null) return;
    g.append(s("line", { x1: 0, x2: W - padR, y1: y(v), y2: y(v), class: `lvl ${cls}` }),
      s("rect", { x: W - padR, y: y(v) - 9, width: padR, height: 18, class: `tagbg ${cls}` }),
      s("text", { x: W - padR + 4, y: y(v) + 4, class: "tagtxt" }, label));
  };
  level(c.resistance, "res", "BRK");
  if (pos) { level(pos.entry, "entry", "BUY"); level(pos.stop, "stop", "STOP"); if (pos.target) level(pos.target, "target", "TP"); }
  if (c.last != null) level(c.last, "last", fmtP(c.last));
  const t0 = c.candles[0][0], tf = n > 1 ? c.candles[1][0] - t0 : 1;
  for (const mk of c.markers) {  // buy and sell arrows
    const i = Math.min(n - 1, Math.max(0, Math.floor((mk.t - t0) / tf)));
    const buy = mk.side === "buy", yy = y(mk.price) + (buy ? 14 : -14);
    g.append(s("path", { d: buy ? `M${x(i)},${yy - 10} l-6,10 h12 z` : `M${x(i)},${yy + 10} l-6,-10 h12 z`, class: `mk ${buy ? "up" : "down"}` },
      s("title", {}, `${buy ? "Bought" : "Sold"} at ${fmtP(mk.price)}`)));
  }
  const hover = s("line", { x1: 0, x2: 0, y1: 0, y2: H - padB, class: "cross", visibility: "hidden" });
  g.append(hover);
  const show = (i) => {
    const k = c.candles[i];
    info.textContent = `${new Date(k[0]).toLocaleString()}  O ${fmtP(k[1])}  H ${fmtP(k[2])}  L ${fmtP(k[3])}  C ${fmtP(k[4])}  Vol ${fmtP(k[5])}`;
  };
  g.addEventListener("mousemove", (e) => {
    const r = g.getBoundingClientRect();
    const i = Math.min(n - 1, Math.max(0, Math.floor((e.clientX - r.left) / r.width * W / step)));
    hover.setAttribute("x1", x(i)); hover.setAttribute("x2", x(i)); hover.setAttribute("visibility", "visible"); show(i);
  });
  g.addEventListener("mouseleave", () => { hover.setAttribute("visibility", "hidden"); show(n - 1); });
  show(n - 1);
  return g;
}

// --- order panel ------------------------------------------------------------------
function orderPanel() {
  const panel = h("div", { class: "t-order t-card" });
  const sig = trade.chart ? trade.chart.signal : null;
  const pos = trade.chart ? trade.chart.position : null;
  const tabs = h("div", { class: "t-side" },
    h("button", { class: trade.side === "buy" ? "buy sel" : "", onclick: () => { trade.side = "buy"; trade.result = null; trade.editing = false; renderTrade($("trade")); } }, "Buy"),
    h("button", { class: trade.side === "sell" ? "sell sel" : "", onclick: () => { trade.side = "sell"; trade.result = null; trade.editing = false; renderTrade($("trade")); } }, "Sell"));
  panel.append(tabs);
  if (!trade.account.account) { panel.append(createAccountForm()); return panel; }
  panel.append(trade.side === "buy" ? buyForm(sig) : sellForm(pos));
  if (trade.result) panel.append(trade.result);
  if (sig) panel.append(studyBox(sig));
  return panel;
}

function createAccountForm() {
  const bal = h("input", { type: "number", min: "1", step: "any", value: "10000" });
  const err = h("p", { class: "error" });
  return h("div", {},
    h("p", {}, "Create a practice account to start. It uses pretend USDT, never your real balance."),
    h("label", {}, h("span", {}, "Starting balance (USDT)"), bal),
    h("button", { class: "buy wide", onclick: async () => {
      try { await api("POST", `/api/paper/${trade.cid}/account`, { starting_balance: bal.value }); await loadTrade(); }
      catch (e) { err.textContent = e.message; }
    } }, "Create practice account"), err);
}

function field(label, value, hint) {
  const input = h("input", { type: "number", step: "any", value: value ?? "", oninput: () => { trade.editing = true; } });
  return [h("label", { class: "t-field" }, h("span", {}, label), input, hint ? h("small", {}, hint) : null), input];
}

function buyForm(sig) {
  const setup = sig && sig.status === "BUY_SETUP";
  const [stopL, stop] = field("Stop-loss (sell if price falls to)", sig && sig.suggested_stop, "Suggested by the chart study");
  const [tpL, tp] = field("Take-profit (sell if price rises to)", sig && sig.suggested_target, null);
  const [riskL, risk] = field("Risk per trade (%)", "", "Empty = your maximum from Safety settings. Size is worked out from this.");
  const btn = h("button", { class: "buy wide", onclick: async () => {
    btn.disabled = true;
    trade.editing = false;
    const edited = stop.value !== String(sig && sig.suggested_stop) || tp.value !== String(sig && sig.suggested_target);
    const body = { symbol: trade.symbol, source: setup ? "strategy" : "manual" };
    if (!setup || edited) { body.stop_price = stop.value; body.take_profit_price = tp.value; }
    if (risk.value) body.risk_pct = risk.value;
    try { showResult(await api("POST", `/api/paper/${trade.cid}/orders`, body)); await loadTrade(false); }
    catch (e) { trade.result = h("p", { class: "error" }, e.message); renderTrade($("trade")); }
  } }, `Buy ${base(trade.symbol)}`);
  return h("div", {},
    h("p", { class: `t-hint ${setup ? "up" : "warn"}` }, setup
      ? "The chart study found a buy setup on this pair."
      : "No buy setup on this pair right now. You can still buy, but the safety checks will warn or block."),
    h("div", { class: "t-price-row" }, h("span", {}, "Price"), h("strong", {}, `${fmtP(trade.chart && trade.chart.ask)} USDT`), h("small", {}, "market (ask)")),
    stopL, tpL, riskL, btn);
}

function sellForm(pos) {
  if (!pos) return h("p", { class: "muted" }, `You don't hold any ${base(trade.symbol)} in the practice account.`);
  const last = trade.chart.bid ? Number(trade.chart.bid) : null;
  const pnl = last != null ? (last - pos.entry) * Number(pos.qty) : null;
  const btn = h("button", { class: "sell wide", onclick: async () => {
    if (!confirm(`Sell all ${pos.qty} ${base(trade.symbol)} at the market price?`)) return;
    btn.disabled = true;
    try {
      const f = await api("POST", `/api/paper/${trade.cid}/positions/${encodeURIComponent(trade.symbol)}/close`);
      trade.result = h("div", { class: "t-result ok" }, `Sold ${f.qty} at ${fmtP(f.price)}. Profit/loss ${signed(f.realized_pnl)} USDT.`);
      await loadTrade(false);
    } catch (e) { trade.result = h("p", { class: "error" }, e.message); renderTrade($("trade")); }
  } }, `Sell all ${base(trade.symbol)}`);
  return h("div", {},
    h("dl", { class: "t-dl" },
      h("dt", {}, "Amount"), h("dd", {}, `${fmtP(pos.qty)} ${base(trade.symbol)}`),
      h("dt", {}, "Bought at"), h("dd", {}, fmtP(pos.entry)),
      h("dt", {}, "Price now"), h("dd", {}, fmtP(last)),
      h("dt", {}, "Profit/loss"), h("dd", { class: updown(pnl) }, pnl == null ? "-" : signed(pnl)),
      h("dt", {}, "Auto-sell below"), h("dd", { class: "down" }, fmtP(pos.stop)),
      h("dt", {}, "Auto-sell above"), h("dd", { class: "up" }, fmtP(pos.target))),
    btn);
}

function showResult(r) {
  const d = r.decision;
  trade.result = h("div", { class: `t-result ${r.fill ? "ok" : "bad"}` },
    h("strong", {}, r.fill ? `Bought ${fmtP(r.fill.qty)} at ${fmtP(r.fill.price)}` : d.status === "WAIT" ? "Not now: waiting" : "Blocked by safety checks"),
    h("div", {}, d.summary));
}

function studyBox(sig) {
  return h("div", { class: "t-study" },
    h("div", { class: "t-head" }, h("span", {}, "Chart study"), h("strong", { class: sig.score >= 80 ? "up" : sig.score >= 60 ? "warn" : "muted" }, `${sig.score}/100`)),
    h("ul", {}, sig.checks.map((c) => h("li", { class: c.ok ? "up" : "down" }, h("b", {}, c.ok ? "✓ " : "✗ "), c.text.replace(/ \(.*\)$/, "")))));
}

// --- bottom: positions, history, auto-trade log -----------------------------------
function bottomPanel() {
  const a = trade.account;
  const which = trade.bottom || "positions";
  const tab = (k, label) => h("button", { class: which === k ? "sel" : "", onclick: () => { trade.bottom = k; renderTrade($("trade")); } }, label);
  let body;
  if (which === "positions") {
    body = a.positions && a.positions.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Pair", "Amount", "Bought at", "Price now", "Profit/loss", "Auto-sell below", "Auto-sell above", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, a.positions.map((p) => h("tr", {},
        h("td", {}, h("button", { class: "link", onclick: () => { trade.symbol = p.symbol; loadTrade(false); } }, p.symbol)),
        h("td", {}, fmtP(p.qty)), h("td", {}, fmtP(p.avg_price)), h("td", {}, fmtP(p.mark)),
        h("td", { class: updown(Number(p.unrealized_pnl)) }, signed(p.unrealized_pnl)),
        h("td", { class: "down" }, fmtP(p.stop_price)), h("td", { class: "up" }, fmtP(p.take_profit)),
        h("td", {}, h("button", { class: "sell small", onclick: () => { trade.symbol = p.symbol; trade.side = "sell"; loadTrade(false); } }, "Sell"))))))
      : h("p", { class: "muted" }, "No open positions.");
  } else if (which === "history") {
    const fills = (a.fills || []).slice(0, 30);
    body = fills.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Time", "Pair", "Side", "Amount", "Price", "Profit/loss", "Why"].map((x) => h("th", {}, x)))),
      h("tbody", {}, fills.map((f) => h("tr", {},
        h("td", {}, timeOnly(f.at)), h("td", {}, f.symbol), h("td", { class: f.side === "buy" ? "up" : "down" }, f.side.toUpperCase()),
        h("td", {}, fmtP(f.qty)), h("td", {}, fmtP(f.price)),
        h("td", { class: f.side === "sell" ? updown(Number(f.realized_pnl)) : "" }, f.side === "sell" ? signed(f.realized_pnl) : ""),
        h("td", {}, f.side === "buy" ? "Entry" : ({ stop_loss: "Stop-loss", take_profit: "Take-profit", manual_close: "Sold by you",
          strategy_exit: "Trend weakened" }[f.reason] || f.reason))))))
      : h("p", { class: "muted" }, "No trades yet.");
  } else {
    const ap = trade.auto;
    body = ap && ap.on
      ? h("ul", { class: "pair-checks" }, ap.pairs.map((p) => {
        const [label, cls] = AUTOPILOT_STATE[p.state] || [p.state, ""];
        return h("li", {}, h("div", {}, h("strong", {}, p.symbol), " ", h("span", { class: `chip ${cls}` }, label),
          h("span", { class: "muted" }, ` · ${timeOnly(p.at)}`)), h("div", { class: "why" }, p.text));
      }))
      : h("p", { class: "muted" }, "Auto-trade is off. Switch it on at the top right to let the app buy and sell by itself.");
  }
  return h("div", { class: "t-bottom t-card" },
    h("div", { class: "t-tabs" }, tab("positions", `Open positions (${(a.positions || []).length})`), tab("history", "Trade history"), tab("auto", "Auto-trade log")),
    body);
}
