"""Text helpers: cleaning, canonical URLs, near-duplicate similarity, red-flag detection."""
from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_BODY_CHARS = 8000

_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_src$|cmpid$)")
_STOP = frozenset("a an and are as at be by for from has have in is it its of on or that the this to was were will with".split())

HEDGES = (
    "reportedly", "rumor", "rumour", "sources say", "according to sources", "people familiar", "unconfirmed",
    "allegedly", "speculat", "could be", "may be planning", "is said to", "unverified", "claims that",
)
DENIALS = ("denies", "denied", "false report", "fake", "not true", "debunk", "no evidence", "refutes", "clarifies that")

# Text that tries to instruct an automated reader. Flagged as manipulation; it never changes behaviour.
INJECTION_PATTERNS = [re.compile(p, re.I) for p in (
    r"ignore (all |any )?(previous|prior|above) (instructions|prompts?)",
    r"disregard (the |all )?(system|previous|above)",
    r"\b(you are|act as) (now )?(an? )?(ai|assistant|trading bot|system)\b",
    r"\b(set|change|raise|increase|disable) (the )?(risk|stop[- ]?loss|position size|leverage|kill switch|emergency stop)",
    r"\b(buy|sell) (now|immediately|all)\b.*\b(bot|algorithm|ai)\b",
    r"\b(api[_ ]?key|secret key|passphrase|private key|seed phrase)\b",
    r"</?(system|instruction|prompt|untrusted_article)[^>]*>",
)]


def clean_text(raw: str, limit: int = MAX_BODY_CHARS) -> str:
    """Strip tags and control characters; collapse whitespace; bound length."""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw or "", flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not _TRACKING.match(k.lower())))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9$]+", text.lower()) if w not in _STOP and len(w) > 1}


def headline_hash(headline: str) -> str:
    return hashlib.sha256(" ".join(sorted(tokens(headline))).encode()).hexdigest()[:32]


def similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def is_hedged(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in HEDGES)


def is_denial(text: str) -> bool:
    low = text.lower()
    return any(d in low for d in DENIALS)


def injection_flags(text: str) -> list[str]:
    return [p.pattern for p in INJECTION_PATTERNS if p.search(text)]
