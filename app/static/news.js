// News intelligence dashboard. Uses h(), api(), fmtTime(), safeUrl() and exName() from app.js.
// All article text is untrusted: it is rendered with textContent only, and links must be http(s).
"use strict";

const CATEGORY_LABEL = { A: "Crypto-specific", B: "Regulatory", C: "Macro", D: "Market" };
const SENTIMENT_LABEL = {
  strongly_positive: "Strongly positive", moderately_positive: "Moderately positive", neutral: "Neutral",
  moderately_negative: "Moderately negative", strongly_negative: "Strongly negative", uncertain: "Uncertain / conflicting",
};
const CONFIRMATION_LABEL = {
  confirmed: "Confirmed", credible_unconfirmed: "Credible, unconfirmed", rumor: "Rumor", disputed: "Disputed",
};
const TIER_LABEL = { 1: "Official", 2: "Reputable outlet", 3: "Aggregator", 4: "Social / unknown" };
const newsFilters = {};

const badge = (cls, text) => h("span", { class: `badge ${cls}` }, text);
const link = (url, text) => (safeUrl(url) ? h("a", { href: safeUrl(url), target: "_blank", rel: "noopener noreferrer" }, text) : text);
const options = (obj, all) => [h("option", { value: "" }, all), ...Object.entries(obj).map(([v, l]) => h("option", { value: v }, l))];

async function renderNews() {
  const root = $("news");
  const [overview, calendar, perf] = await Promise.all([
    api("GET", "/api/news/overview"), api("GET", "/api/news/calendar"), api("GET", "/api/news/performance")]);
  const feedBox = h("div");
  const pollOut = h("span", { class: "note" });
  root.replaceChildren(h("section", { class: "panel section" },
    h("h2", {}, "News intelligence"),
    h("p", { class: "note" }, "Safety-only mode: news can block, delay or shrink a trade. It never approves a trade, " +
      "raises a position size, widens a stop or turns off emergency controls."),
    h("div", { class: "actions" },
      h("button", { onclick: async (e) => {
        e.target.disabled = true; pollOut.textContent = "Checking sources…";
        try {
          const r = await api("POST", "/api/news/poll");
          const ok = Object.values(r).filter((x) => x.ok).length;
          const added = Object.values(r).reduce((n, x) => n + (x.new || 0), 0);
          pollOut.textContent = `${ok} of ${Object.keys(r).length} sources answered, ${added} new item(s).`;
          await renderNews();
        } catch (err) { pollOut.textContent = err.message; } finally { e.target.disabled = false; }
      } }, "Check news now"), pollOut),
    newsVsTechnicals(overview.assets),
    h("h3", {}, "Latest news"),
    feedFilters(overview.sources, () => loadFeed(feedBox)),
    feedBox,
    calendarBox(calendar),
    actionsBox(overview.actions),
    perfBox(perf),
    sourcesBox(overview.sources),
    newsSettings(overview.config)));
  await loadFeed(feedBox);
}

function newsVsTechnicals(rows) {
  if (!rows.length) return h("p", { class: "muted" }, "Enable paper trading on a connection to see news next to its trading pairs.");
  return h("div", {}, h("h3", {}, "News vs technicals by pair"),
    h("table", {},
      h("thead", {}, h("tr", {}, ["Pair", "Exchange", "Technical", "News check", "Size", "Why"].map((x) => h("th", {}, x)))),
      h("tbody", {}, rows.map((r) => {
        const n = r.news || { result: "warn", detail: "News engine unavailable.", size_multiplier: 1 };
        return h("tr", {},
          h("td", {}, r.symbol), h("td", {}, exName(r.exchange)),
          h("td", { class: `sig ${(r.technical.status || "").toLowerCase()}` }, (r.technical.status || "-").replace(/_/g, " ")),
          h("td", { class: `effect-${n.result}` }, n.result.toUpperCase()),
          h("td", {}, `${Math.round(n.size_multiplier * 100)}%`),
          h("td", { class: "why" }, n.detail));
      }))));
}

function feedFilters(sources, reload) {
  const set = (k) => (e) => { newsFilters[k] = e.target.value; reload(); };
  const sel = (k, opts) => h("select", { onchange: set(k), "aria-label": k }, opts);
  return h("div", { class: "row filters" },
    h("input", { type: "text", placeholder: "Asset (e.g. BTC)", "aria-label": "asset", onchange: set("asset") }),
    sel("exchange", options({ okx: "OKX", kucoin: "KuCoin" }, "Any exchange")),
    sel("category", options(CATEGORY_LABEL, "Any category")),
    sel("sentiment", options(SENTIMENT_LABEL, "Any sentiment")),
    sel("confirmation", options(CONFIRMATION_LABEL, "Any status")),
    sel("severity", options({ critical: "Critical", high: "High", medium: "Medium", low: "Low" }, "Any severity")),
    sel("source", options(Object.fromEntries(sources.map((s) => [s.id, s.name])), "Any source")),
    h("input", { type: "date", "aria-label": "since", onchange: (e) => { newsFilters.since = e.target.value ? new Date(e.target.value).toISOString() : ""; reload(); } }));
}

async function loadFeed(box) {
  const q = new URLSearchParams(Object.entries(newsFilters).filter(([, v]) => v)).toString();
  try {
    const events = await api("GET", `/api/news${q ? `?${q}` : ""}`);
    box.replaceChildren(...(events.length ? events.map(eventCard)
      : [h("p", { class: "muted" }, "No news yet. Sources are checked every few minutes, or use “Check news now”.")]));
  } catch (e) { box.replaceChildren(h("p", { class: "error" }, e.message)); }
}

function eventCard(ev) {
  const first = ev.items[0] || {};
  const cls = (ev.classifications || [])[0] || {};
  const assets = Object.entries(ev.assets).map(([a, how]) => (how === "model" ? `${a} (attribution uncertain)` : a));
  return h("article", { class: `news-event sev-${ev.severity}` },
    h("div", { class: "news-head" },
      h("strong", {}, link(first.url, ev.title)),
      h("div", { class: "badges" },
        badge(`sent ${ev.sentiment}`, SENTIMENT_LABEL[ev.sentiment]),
        badge(`conf ${ev.confirmation}`, CONFIRMATION_LABEL[ev.confirmation]),
        badge(`sev ${ev.severity}`, `${ev.severity} impact`),
        badge("cat", `${CATEGORY_LABEL[ev.category]} · ${ev.subcategory.replace(/_/g, " ")}`),
        ev.is_repeat_of ? badge("repeat", "Repeat of older news") : null)),
    h("p", { class: "note" },
      `${ev.best_source} (${TIER_LABEL[ev.best_tier]}) · published ${first.published_at ? fmtTime(first.published_at) : "time not given"}` +
      ` · detected ${fmtTime(ev.first_seen_at)} · horizon ${ev.horizon} · confidence ${Math.round(ev.confidence * 100)}%` +
      ` · novelty ${ev.novelty === 1 ? "new" : "low"}`),
    h("p", { class: "note" }, `Assets: ${assets.join(", ") || "market-wide / none"}` +
      (ev.exchanges.length ? ` · Exchanges: ${ev.exchanges.map(exName).join(", ")}` : "") + ` · ${ev.confirmation_reason}`),
    ev.reactions && ev.reactions.length ? h("p", { class: "note" }, "Market reaction since detection: " + ev.reactions.map((r) =>
      `${r.symbol} on ${exName(r.exchange)} ${r.change_pct === null ? "no current price" : `${r.change_pct > 0 ? "+" : ""}${r.change_pct}%`}`).join(", ")) : null,
    h("details", {}, h("summary", {}, `Facts and interpretation (${ev.items.length} source${ev.items.length > 1 ? "s" : ""})`),
      h("p", {}, h("strong", {}, "Stated in the source")),
      cls.stated_facts && cls.stated_facts.length
        ? h("ul", {}, cls.stated_facts.map((f) => h("li", {}, f.fact, h("q", { class: "muted" }, f.quote))))
        : h("p", { class: "muted" }, "No verifiable facts extracted."),
      h("p", {}, h("strong", {}, cls.model_id === "keyword-rules" ? "Rough interpretation (keyword rules)" : `AI interpretation (${cls.model_id}), not fact`)),
      h("p", {}, cls.interpretation || "-"),
      cls.note ? h("p", { class: "warn" }, cls.note) : null,
      cls.injection_flags && cls.injection_flags.length ? h("p", { class: "error" }, "Contains text aimed at automated readers. Treated as unreliable.") : null,
      h("p", {}, h("strong", {}, "All sources")),
      h("ul", {}, ev.items.map((i) => h("li", {}, link(i.url, i.headline),
        ` · ${i.source_name} · ${TIER_LABEL[i.tier]} · published ${i.published_at ? fmtTime(i.published_at) : "?"}` +
        (i.detect_latency_s !== undefined ? ` · detected after ${Math.round(i.detect_latency_s / 60)} min` : ""))))));
}

function calendarBox(entries) {
  const name = h("input", { type: "text", placeholder: "Event name", required: true });
  const when = h("input", { type: "datetime-local", required: true, "aria-label": "time" });
  const impact = h("select", {}, ["high", "medium", "low"].map((x) => h("option", { value: x }, `${x} impact`)));
  const assets = h("input", { type: "text", placeholder: "Assets (blank = all)" });
  const src = h("input", { type: "text", placeholder: "https:// link stating the date and time", required: true });
  const verified = h("input", { type: "checkbox" });
  const err = h("p", { class: "error" });
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return h("div", {}, h("h3", {}, "Scheduled events"),
    h("p", { class: "note" }, `Times shown in your timezone (${tz}). Entries need a source link; unverified or stale times are marked uncertain. ` +
      "The calendar never predicts an outcome."),
    entries.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["When", "Event", "Assets", "Impact", "Status", "Source", ""].map((x) => h("th", {}, x)))),
      h("tbody", {}, entries.map((c) => h("tr", {},
        h("td", {}, fmtTime(c.scheduled_at)), h("td", {}, c.name, c.uncertain ? h("span", { class: "warn" }, " (uncertain)") : null),
        h("td", {}, c.assets.join(", ") || "all"), h("td", {}, c.impact),
        h("td", {}, h("select", { onchange: async (e) => { await api("PUT", `/api/news/calendar/${c.id}`, { status: e.target.value }); renderNews(); } },
          ["scheduled", "confirmed", "changed", "completed", "cancelled"].map((s) => h("option", { value: s, selected: s === c.status }, s)))),
        h("td", {}, link(c.source_url, "source")),
        h("td", {}, h("button", { class: "link", onclick: async () => { await api("DELETE", `/api/news/calendar/${c.id}`); renderNews(); } }, "Remove"))))))
      : h("p", { class: "muted" }, "No scheduled events yet."),
    h("form", { class: "row", onsubmit: async (e) => {
      e.preventDefault(); err.textContent = "";
      try {
        await api("POST", "/api/news/calendar", {
          name: name.value, scheduled_at: new Date(when.value).toISOString(), impact: impact.value,
          assets: assets.value.split(/[,\s]+/).filter(Boolean), source_url: src.value, time_verified: verified.checked,
        });
        renderNews();
      } catch (x) { err.textContent = x.message; }
    } }, name, when, impact, assets, src,
      h("label", { class: "inline" }, verified, " Time verified from the source"),
      h("button", { type: "submit" }, "Add event")),
    err);
}

function actionsBox(actions) {
  return h("details", {}, h("summary", {}, `What news has done (${actions.length})`),
    actions.length ? h("ul", {}, actions.map((a) => h("li", {}, `${fmtTime(a.at)} · ${a.action.split(":")[0].replace(/_/g, " ")} · ${a.detail}`)))
      : h("p", { class: "muted" }, "Nothing yet."));
}

function perfBox(p) {
  const row = (k, label) => h("tr", {}, h("td", {}, label), ...["trades", "wins", "net_pnl", "avg_pnl"].map((f) => h("td", {}, p[k][f] ?? "-")));
  return h("details", {}, h("summary", {}, "News vs technical-only results (paper)"),
    h("table", {}, h("thead", {}, h("tr", {}, ["", "Closed trades", "Wins", "Net P&L", "Avg P&L"].map((x) => h("th", {}, x)))),
      h("tbody", {}, row("technical_only", "Technical only"), row("news_in_play", "News in play"))),
    h("p", { class: "note" }, p.note));
}

function sourcesBox(sources) {
  return h("details", {}, h("summary", {}, "News sources"),
    h("p", { class: "note" }, "Only these allowlisted sources are read. Commercial outlets start switched off: check their feed terms before turning them on."),
    h("table", {},
      h("thead", {}, h("tr", {}, ["On", "Source", "Reliability", "Last success", "Last problem", "Note"].map((x) => h("th", {}, x)))),
      h("tbody", {}, sources.map((s) => h("tr", {},
        h("td", {}, h("input", { type: "checkbox", checked: s.enabled, "aria-label": `enable ${s.name}`, onchange: async (e) => {
          await api("PUT", `/api/news/sources/${s.id}`, { enabled: e.target.checked }); renderNews();
        } })),
        h("td", {}, link(s.url, s.name)), h("td", {}, `Tier ${s.tier} · ${TIER_LABEL[s.tier]}`),
        h("td", {}, fmtTime(s.last_ok_at)), h("td", { class: "error" }, s.last_error || ""), h("td", { class: "note" }, s.note))))));
}

function newsSettings(cfg) {
  const labels = {
    max_news_age_hours: "Ignore news older than (hours)", negative_block_hours: "Block buys after confirmed bad news (hours)",
    confirmation_candles: "Confirmation window (strategy candles)", min_confirmation_minutes: "Min confirmation window (min)",
    max_confirmation_minutes: "Max confirmation window (min)", uncertain_size_multiplier: "Size multiplier for uncertain news (0-1)",
    volatile_size_multiplier: "Size multiplier for volatile news (0-1)", feeds_stale_minutes: "Warn when feeds are silent for (min)",
    cluster_window_hours: "Group reports within (hours)", repeat_lookback_days: "Spot repeated news over (days)",
    poll_interval_seconds: "Check sources every (s)", calendar_stale_days: "Calendar entry stale after (days)",
  };
  const inputs = {};
  const flags = {};
  const err = h("p", { class: "error" });
  const win = {};
  return h("details", {}, h("summary", {}, "News settings"),
    h("div", { class: "grid2" }, Object.keys(labels).map((k) => {
      inputs[k] = h("input", { type: "number", step: "any", value: cfg[k] });
      return h("label", {}, h("span", {}, labels[k]), inputs[k]);
    })),
    h("h4", {}, "No-trade windows around scheduled events (minutes before / after)"),
    h("div", { class: "grid2" }, ["high", "medium", "low"].map((lvl) => {
      win[lvl] = [h("input", { type: "number", min: "0", value: cfg.no_trade_windows[lvl].before_minutes, "aria-label": `${lvl} before` }),
                  h("input", { type: "number", min: "0", value: cfg.no_trade_windows[lvl].after_minutes, "aria-label": `${lvl} after` })];
      return h("label", {}, h("span", {}, `${lvl} impact`), h("div", { class: "row" }, win[lvl]));
    })),
    ...[["auto_suspend_on_incidents", "Suspend entries automatically on credible security incidents and exchange disruptions"],
        ["block_when_feeds_stale", "Block entries (instead of warning) when news feeds are silent"]].map(([k, l]) => {
      flags[k] = h("input", { type: "checkbox", checked: cfg[k] });
      return h("label", { class: "inline" }, flags[k], ` ${l}`);
    }),
    h("button", { onclick: async () => {
      err.textContent = "";
      const body = { ...cfg,
        ...Object.fromEntries(Object.entries(inputs).map(([k, el]) => [k, Number(el.value)])),
        ...Object.fromEntries(Object.entries(flags).map(([k, el]) => [k, el.checked])),
        no_trade_windows: Object.fromEntries(Object.entries(win).map(([lvl, [b, a]]) => [lvl, { before_minutes: Number(b.value), after_minutes: Number(a.value) }])),
      };
      try { await api("PUT", "/api/news/config", body); renderNews(); } catch (e) { err.textContent = e.message; }
    } }, "Save news settings"), err);
}
