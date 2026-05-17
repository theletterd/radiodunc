from __future__ import annotations

import logging
import random
import re
import urllib.error
import urllib.request
from html import unescape

logger = logging.getLogger(__name__)

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_CDATA_RE = re.compile(r"^<!\[CDATA\[(.*?)\]\]>$", re.DOTALL)


def fetch_random_headline(rss_url: str) -> str | None:
    """Fetch an RSS feed and return one randomly-chosen headline, or None on failure.

    Skips the first <title> in the feed (which is the channel title, not an item).
    """
    logger.info("Fetching RSS feed for headline", extra={"rss_url": rss_url})
    try:
        req = urllib.request.Request(rss_url, headers={"User-Agent": "RadioDunc/0.3"})
        with urllib.request.urlopen(req, timeout=15) as response:  # noqa: S310
            body = response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.warning("RSS fetch failed", extra={"rss_url": rss_url, "error": str(exc)})
        return None

    raw_titles = _TITLE_RE.findall(body)
    # Drop the channel title (first match) and strip CDATA/HTML entities
    cleaned = []
    for raw in raw_titles[1:]:
        match = _CDATA_RE.match(raw.strip())
        text = unescape((match.group(1) if match else raw).strip())
        if text:
            cleaned.append(text)

    if not cleaned:
        logger.warning("RSS feed had no item titles", extra={"rss_url": rss_url})
        return None

    headline = random.choice(cleaned)
    logger.info("Selected RSS headline", extra={"rss_url": rss_url, "headline": headline})
    return headline
