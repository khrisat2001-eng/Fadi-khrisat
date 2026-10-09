"""News engine through the API, including its effect on paper orders."""
from decimal import Decimal as D

from .test_paper_api import buy, paper, price  # noqa: F401 - fixture

OKX_HACK = (b'{"code":"0","data":[{"details":[{"annType":"announcements-latest","pTime":"%d",'
            b'"title":"OKX hot wallet exploited; withdrawals paused","url":"https://www.okx.com/help/incident"}]}]}')


def poll(client):
    return client.post("/api/news/poll").json()


def test_poll_records_source_health(client, news_feed):
    import time
    news_feed.payloads["okx-announcements"] = OKX_HACK % int(time.time() * 1000)
    result = poll(client)
    assert result["okx-announcements"] == {"ok": True, "new": 1, "seen": 1}
    assert result["sec-press"]["ok"] is False and "offline" in result["sec-press"]["error"]
    assert "coindesk" not in result  # disabled until its terms are checked
    sources = {s["id"]: s for s in client.get("/api/news/overview").json()["sources"]}
    assert sources["okx-announcements"]["last_ok_at"] and sources["sec-press"]["last_error"]
    assert poll(client)["okx-announcements"]["new"] == 0  # dedup across polls


def test_news_incident_blocks_paper_buys(client, paper, news_feed):  # noqa: F811
    import time
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    news_feed.payloads["okx-announcements"] = OKX_HACK % int(time.time() * 1000)
    poll(client)
    [s] = client.get("/api/risk/suspensions").json()
    assert s["scope"] == "exchange" and s["target"] == "okx"
    r = buy(client, paper)
    assert r["decision"]["status"] == "REJECT" and r["fill"] is None
    checks = {c["name"]: c for c in r["decision"]["checks"]}
    assert checks["suspensions"]["result"] == "fail"
    [dec] = client.get(f"/api/decisions?connection_id={paper}").json()
    ev = dec["inputs"]["news"]["events"][0]
    assert ev["url"] == "https://www.okx.com/help/incident" and ev["scope"] == "exchange"
    # Lifting the suspension is the user's call; afterwards the news is still shown on the decision.
    client.delete(f"/api/risk/suspensions/{s['id']}")
    price(client, 100)
    r = buy(client, paper)
    assert r["decision"]["status"] == "APPROVE"
    assert {c["name"]: c for c in r["decision"]["checks"]}["news"]["result"] == "warn"


def test_feed_filters_and_links(client):
    news = client.app_ref.state.news
    import asyncio

    from app.news.models import RawItem
    for src, title in [("sec-press", "SEC charges Solana promoter"), ("fed-press", "FOMC statement on interest rate")]:
        asyncio.run(news.ingest(RawItem(src, f"https://{src}.test/{len(title)}", title, "", None)))
    allnews = client.get("/api/news").json()
    assert len(allnews) == 2 and all(e["items"][0]["url"].startswith("https://") for e in allnews)
    assert "body" not in allnews[0]["items"][0]
    sol = client.get("/api/news?asset=SOL").json()
    assert [e["title"] for e in sol] == ["SEC charges Solana promoter"]
    assert client.get("/api/news?category=C").json()[0]["subcategory"] == "central_bank"
    assert client.get("/api/news?source=fed-press").json()[0]["title"].startswith("FOMC")


def test_sources_can_be_toggled(client):
    r = client.put("/api/news/sources/coindesk", json={"enabled": True}).json()
    assert next(s for s in r if s["id"] == "coindesk")["enabled"] is True
    assert client.put("/api/news/sources/nope", json={"enabled": True}).status_code == 404


def test_config_validation(client):
    cfg = client.get("/api/news/config").json()
    cfg["uncertain_size_multiplier"] = 2
    assert client.put("/api/news/config", json=cfg).status_code == 422
    cfg["uncertain_size_multiplier"] = 0.25
    assert client.put("/api/news/config", json=cfg).json()["uncertain_size_multiplier"] == 0.25


def test_calendar_api(client):
    bad = client.post("/api/news/calendar", json={"name": "CPI", "scheduled_at": "2026-11-12T13:30:00Z", "impact": "high",
                                                   "source_url": "not a url"})
    assert bad.status_code == 400 and "link" in bad.json()["error"]
    cal = client.post("/api/news/calendar", json={"name": "CPI", "scheduled_at": "2026-11-12T13:30:00Z", "impact": "high",
                                                   "source_url": "https://www.bls.gov/schedule/news_release/cpi.htm",
                                                   "time_verified": True}).json()
    assert cal[0]["scheduled_at"] == "2026-11-12T13:30:00+00:00" and cal[0]["uncertain"] is False
    moved = client.put(f"/api/news/calendar/{cal[0]['id']}", json={"scheduled_at": "2026-11-13T13:30:00Z"}).json()
    assert moved[0]["status"] == "changed"
    assert client.delete(f"/api/news/calendar/{cal[0]['id']}").json() == []


def test_overview_shows_news_next_to_technicals(client, paper):  # noqa: F811
    o = client.get("/api/news/overview").json()
    rows = {a["symbol"]: a for a in o["assets"]}
    assert set(rows) == {"BTC-USDT", "ETH-USDT"}
    assert rows["BTC-USDT"]["news"]["result"] == "warn"  # feeds haven't updated yet
    assert rows["BTC-USDT"]["technical"]["status"]


def test_performance_split(client, paper):  # noqa: F811
    client.post(f"/api/paper/{paper}/account", json={"starting_balance": "10000"})
    price(client, 100)
    assert buy(client, paper)["decision"]["status"] == "APPROVE"
    client.post(f"/api/paper/{paper}/positions/BTC-USDT/close")
    perf = client.get("/api/news/performance").json()
    # Feeds never updated in tests, but no news event was in play, so this counts as technical-only.
    assert perf["technical_only"]["trades"] == 1 and perf["news_in_play"]["trades"] == 0
    assert D(perf["technical_only"]["net_pnl"]) < 0  # fees and slippage on a flat round trip
