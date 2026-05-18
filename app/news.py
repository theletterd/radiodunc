from __future__ import annotations

import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from html import unescape

logger = logging.getLogger(__name__)

# 30-min RSS feed cache. The news bulletin cache in main.py already gates
# most calls, but this guards against repeat fetches across server restarts
# and during cold-start back-to-back generations.
_HEADLINES_TTL_S = 30 * 60
_headlines_cache: dict[tuple, tuple[float, dict]] = {}
_headlines_cache_lock = threading.Lock()

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_ITEM_RE = re.compile(r"<item[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)
_DESC_RE = re.compile(r"<description[^>]*>(.*?)</description>", re.IGNORECASE | re.DOTALL)
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*?)\]\]>$", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_cdata(text: str) -> str:
    text = text.strip()
    match = _CDATA_RE.match(text)
    return (match.group(1) if match else text).strip()


def _clean_html(text: str) -> str:
    """Strip HTML tags and decode entities. RSS descriptions often contain markup."""
    return unescape(_HTML_TAG_RE.sub("", text)).strip()


def _fetch_rss(rss_url: str) -> str | None:
    logger.debug("Fetching RSS feed", extra={"rss_url": rss_url})
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "RadioDunc/0.3"})
        with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("RSS fetch failed", extra={"rss_url": rss_url, "error": str(exc)})
        return None


def fetch_random_headline(rss_url: str) -> str | None:
    """Fetch an RSS feed and return one randomly-chosen headline, or None on failure.

    Skips the first <title> in the feed (which is the channel title, not an item).
    """
    body = _fetch_rss(rss_url)
    if body is None:
        return None

    raw_titles = _TITLE_RE.findall(body)
    cleaned = [t for t in (unescape(_strip_cdata(raw)) for raw in raw_titles[1:]) if t]
    if not cleaned:
        logger.warning("RSS feed had no item titles", extra={"rss_url": rss_url})
        return None

    headline = random.choice(cleaned)
    logger.debug("Selected RSS headline", extra={"rss_url": rss_url, "headline": headline})
    return headline


def fetch_top_headlines(rss_url: str, count: int = 3) -> dict | None:
    """Fetch top N RSS items with title + description, plus the channel title.

    Returns None on failure. On success: {'source': str, 'items': [{'title', 'description'}, ...]}.
    Items are returned in feed order (most RSS feeds put newest first). Successful
    results are cached for 30 minutes per (rss_url, count) — failures are not cached.
    """
    key = (rss_url, count)
    now = time.time()
    with _headlines_cache_lock:
        cached = _headlines_cache.get(key)
    if cached and now - cached[0] < _HEADLINES_TTL_S:
        logger.debug("Headlines cache hit", extra={"rss_url": rss_url, "age_s": round(now - cached[0])})
        return cached[1]

    body = _fetch_rss(rss_url)
    if body is None:
        return None

    # Channel title is the first <title> outside any <item>. Items come later.
    # Simplest reliable approach: grab the first <title> before the first <item>.
    first_item_pos = body.lower().find("<item")
    channel_blob = body[:first_item_pos] if first_item_pos >= 0 else body
    channel_match = _TITLE_RE.search(channel_blob)
    source = unescape(_strip_cdata(channel_match.group(1))) if channel_match else "the news"

    items = []
    for raw_item in _ITEM_RE.findall(body):
        title_m = _TITLE_RE.search(raw_item)
        desc_m = _DESC_RE.search(raw_item)
        if not title_m:
            continue
        title = unescape(_strip_cdata(title_m.group(1)))
        description = _clean_html(_strip_cdata(desc_m.group(1))) if desc_m else ""
        if title:
            items.append({"title": title, "description": description})
        if len(items) >= count:
            break

    if not items:
        logger.warning("RSS feed had no items", extra={"rss_url": rss_url})
        return None

    logger.info(
        "Fetched top headlines",
        extra={"rss_url": rss_url, "source": source, "count": len(items)},
    )
    result = {"source": source, "items": items}
    with _headlines_cache_lock:
        _headlines_cache[key] = (time.time(), result)
    return result
