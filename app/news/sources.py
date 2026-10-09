"""Allowlisted news sources and their parsers.

Only sources in the allowlist are fetched. Each has a reliability tier:
1 official/primary, 2 reputable outlet, 3 aggregator, 4 social/unknown.
Commercial outlets ship disabled: their feed terms must be checked before use.
Fetching is bounded (HTTPS only, timeout, size cap, same-site redirects) and
XML is parsed with defusedxml, because feed content is untrusted.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from defusedxml import ElementTree as ET

from .models import RawItem
from .text import clean_text

MAX_FEED_BYTES = 2_000_000
FETCH_TIMEOUT = 15.0

DEFAULT_SOURCES: list[dict[str, Any]] = [
    {"id": "okx-announcements", "name": "OKX announcements", "kind": "okx_announcements", "tier": 1, "enabled": True,
     "url": "https://www.okx.com/api/v5/support/announcements", "note": "Official OKX public API."},
    {"id": "kucoin-announcements", "name": "KuCoin announcements", "kind": "kucoin_announcements", "tier": 1, "enabled": True,
     "url": "https://api.kucoin.com/api/v3/announcements?pageSize=50&lang=en_US", "note": "Official KuCoin public API."},
    {"id": "sec-press", "name": "SEC press releases", "kind": "rss", "tier": 1, "enabled": True,
     "url": "https://www.sec.gov/news/pressreleases.rss",
     "note": "US regulator. The SEC asks automated clients to send a User-Agent with contact details (set NEWS_USER_AGENT)."},
    {"id": "fed-press", "name": "Federal Reserve press releases", "kind": "rss", "tier": 1, "enabled": True,
     "url": "https://www.federalreserve.gov/feeds/press_all.xml", "note": "US central bank."},
    {"id": "ethereum-blog", "name": "Ethereum Foundation blog", "kind": "rss", "tier": 1, "enabled": True,
     "url": "https://blog.ethereum.org/feed.xml", "note": "Official project blog."},
    {"id": "bitcoin-core-releases", "name": "Bitcoin Core releases", "kind": "rss", "tier": 1, "enabled": True,
     "url": "https://github.com/bitcoin/bitcoin/releases.atom", "note": "Official release feed."},
    {"id": "coindesk", "name": "CoinDesk", "kind": "rss", "tier": 2, "enabled": False,
     "url": "https://www.coindesk.com/arc/outboundfeeds/rss/", "note": "Commercial outlet: check its feed terms before enabling."},
    {"id": "cointelegraph", "name": "Cointelegraph", "kind": "rss", "tier": 2, "enabled": False,
     "url": "https://cointelegraph.com/rss", "note": "Commercial outlet: check its feed terms before enabling."},
]

Fetcher = Callable[[dict[str, Any]], Awaitable[bytes]]


def _site(host: str) -> str:
    return ".".join(host.lower().split(".")[-2:])


async def http_fetch(source: dict[str, Any]) -> bytes:
    url = source["url"]
    if urlsplit(url).scheme != "https":
        raise ValueError("Only HTTPS sources are allowed.")
    headers = {"User-Agent": os.environ.get("NEWS_USER_AGENT", "FadiTradingApp/0.1 news monitor"),
               "Accept": "application/rss+xml, application/atom+xml, application/xml, application/json;q=0.9"}
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True, max_redirects=3, headers=headers) as client:
        async with client.stream("GET", url) as resp:
            if _site(resp.url.host) != _site(urlsplit(url).hostname or ""):
                raise ValueError("Source redirected to a different site; not following.")
            resp.raise_for_status()
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > MAX_FEED_BYTES:
                    raise ValueError("Feed is larger than the size limit.")
                chunks.append(chunk)
            return b"".join(chunks)


# --- parsers ----------------------------------------------------------------------------------

def _http_url(u: str | None) -> str | None:
    u = (u or "").strip()
    return u if urlsplit(u).scheme in ("http", "https") and urlsplit(u).netloc else None


def _date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:
        d = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _ms(v: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(v) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


ATOM = "{http://www.w3.org/2005/Atom}"
CONTENT = "{http://purl.org/rss/1.0/modules/content/}encoded"


def parse_feed(source_id: str, data: bytes) -> list[RawItem]:
    root = ET.fromstring(data)
    items: list[RawItem] = []
    for it in root.iter("item"):  # RSS 2.0
        link = _http_url(it.findtext("link"))
        title = clean_text(it.findtext("title") or "", 400)
        if not link or not title:
            continue
        body = it.findtext(CONTENT) or it.findtext("description") or ""
        items.append(RawItem(source_id, link, title, clean_text(body), _date(it.findtext("pubDate")),
                             external_id=(it.findtext("guid") or link)[:500]))
    for e in root.iter(f"{ATOM}entry"):  # Atom
        link_el = next((l for l in e.findall(f"{ATOM}link") if l.get("rel", "alternate") == "alternate"), None)
        link = _http_url(link_el.get("href") if link_el is not None else None)
        title = clean_text(e.findtext(f"{ATOM}title") or "", 400)
        if not link or not title:
            continue
        body = e.findtext(f"{ATOM}content") or e.findtext(f"{ATOM}summary") or ""
        published = _date(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"))
        items.append(RawItem(source_id, link, title, clean_text(body), published,
                             external_id=(e.findtext(f"{ATOM}id") or link)[:500]))
    return items


def parse_okx(source_id: str, data: bytes) -> list[RawItem]:
    doc = json.loads(data)
    if str(doc.get("code")) != "0":
        raise ValueError(f"OKX announcements returned code {doc.get('code')}")
    out = []
    for page in doc.get("data") or []:
        for d in page.get("details") or []:
            link, title = _http_url(d.get("url")), clean_text(str(d.get("title") or ""), 400)
            if link and title:
                out.append(RawItem(source_id, link, title, "", _ms(d.get("pTime")),
                                   external_id=link, tags=[str(d.get("annType") or "")]))
    return out


def parse_kucoin(source_id: str, data: bytes) -> list[RawItem]:
    doc = json.loads(data)
    if str(doc.get("code")) != "200000":
        raise ValueError(f"KuCoin announcements returned code {doc.get('code')}")
    out = []
    for d in (doc.get("data") or {}).get("items") or []:
        link, title = _http_url(d.get("annUrl")), clean_text(str(d.get("annTitle") or ""), 400)
        if link and title:
            out.append(RawItem(source_id, link, title, clean_text(str(d.get("annDesc") or "")), _ms(d.get("cTime")),
                               external_id=str(d.get("annId") or link), tags=[str(t) for t in d.get("annType") or []]))
    return out


PARSERS = {"rss": parse_feed, "okx_announcements": parse_okx, "kucoin_announcements": parse_kucoin}
