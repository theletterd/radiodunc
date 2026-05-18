import pytest
from unittest.mock import patch

import app.news as _news_mod
from app.news import fetch_random_headline, fetch_top_headlines


@pytest.fixture(autouse=True)
def _reset_headlines_cache():
    """Reset the 30-min RSS cache between tests so mocked urlopen always fires."""
    _news_mod._headlines_cache.clear()
    yield
    _news_mod._headlines_cache.clear()


SAMPLE_RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>BBC News</title>
  <item><title>Council debates park renovation</title></item>
  <item><title><![CDATA[Storm brings unexpected snow to coast]]></title></item>
  <item><title>Local band releases first album</title></item>
</channel>
</rss>"""


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def test_fetch_random_headline_returns_one_item_title():
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(SAMPLE_RSS)):
        headline = fetch_random_headline("https://example.com/rss")
    assert headline in {
        "Council debates park renovation",
        "Storm brings unexpected snow to coast",
        "Local band releases first album",
    }


def test_fetch_random_headline_skips_channel_title():
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(SAMPLE_RSS)):
        for _ in range(10):
            headline = fetch_random_headline("https://example.com/rss")
            assert headline != "BBC News"


def test_fetch_random_headline_decodes_html_entities():
    rss = b"""<?xml version="1.0"?><rss><channel><title>Feed</title>
    <item><title>Tom &amp; Jerry hit screens</title></item></channel></rss>"""
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(rss)):
        headline = fetch_random_headline("https://example.com/rss")
    assert headline == "Tom & Jerry hit screens"


def test_fetch_random_headline_handles_network_failure():
    import urllib.error

    with patch("app.news.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        assert fetch_random_headline("https://example.com/rss") is None


def test_fetch_random_headline_handles_no_items():
    rss = b"<?xml version='1.0'?><rss><channel><title>Empty</title></channel></rss>"
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(rss)):
        assert fetch_random_headline("https://example.com/rss") is None


_SAMPLE_RSS_WITH_DESCRIPTIONS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>The Guardian — World</title>
  <item>
    <title>Storm hits coastal town</title>
    <description><![CDATA[<p>Residents told to <b>stay indoors</b> as winds reach 90mph.</p>]]></description>
  </item>
  <item>
    <title>Council approves new park</title>
    <description>Mayor signs off on a 12-acre green space.</description>
  </item>
  <item>
    <title>Tech firm announces layoffs</title>
    <description>The cuts affect roughly 8% of the workforce.</description>
  </item>
  <item>
    <title>Fourth story we should not see</title>
    <description>Surplus.</description>
  </item>
</channel>
</rss>""".encode("utf-8")


def test_fetch_top_headlines_returns_titles_descriptions_and_source():
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(_SAMPLE_RSS_WITH_DESCRIPTIONS)):
        result = fetch_top_headlines("https://example.com/rss", count=3)
    assert result is not None
    assert result["source"] == "The Guardian — World"
    titles = [item["title"] for item in result["items"]]
    assert titles == [
        "Storm hits coastal town",
        "Council approves new park",
        "Tech firm announces layoffs",
    ]


def test_fetch_top_headlines_strips_html_from_descriptions():
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(_SAMPLE_RSS_WITH_DESCRIPTIONS)):
        result = fetch_top_headlines("https://example.com/rss", count=1)
    assert result["items"][0]["description"] == "Residents told to stay indoors as winds reach 90mph."


def test_fetch_top_headlines_respects_count():
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(_SAMPLE_RSS_WITH_DESCRIPTIONS)):
        result = fetch_top_headlines("https://example.com/rss", count=2)
    assert len(result["items"]) == 2


def test_fetch_top_headlines_handles_network_failure():
    import urllib.error
    with patch("app.news.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")):
        assert fetch_top_headlines("https://example.com/rss", count=3) is None


def test_fetch_top_headlines_returns_none_when_no_items():
    rss = b"<?xml version='1.0'?><rss><channel><title>Empty</title></channel></rss>"
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(rss)):
        assert fetch_top_headlines("https://example.com/rss", count=3) is None


def test_fetch_top_headlines_caches_successful_fetches():
    url = "https://example.com/rss"
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(_SAMPLE_RSS_WITH_DESCRIPTIONS)) as mock_open:
        first = fetch_top_headlines(url, count=3)
        second = fetch_top_headlines(url, count=3)
    assert first is not None
    assert second == first
    assert mock_open.call_count == 1  # second call served from cache


def test_fetch_top_headlines_does_not_cache_failures():
    import urllib.error
    url = "https://example.com/rss"
    with patch("app.news.urllib.request.urlopen", side_effect=urllib.error.URLError("boom")) as mock_open:
        fetch_top_headlines(url, count=3)
        fetch_top_headlines(url, count=3)
    assert mock_open.call_count == 2  # both calls hit the network


def test_fetch_top_headlines_cache_keyed_separately_per_count():
    url = "https://example.com/rss"
    with patch("app.news.urllib.request.urlopen", return_value=_FakeResponse(_SAMPLE_RSS_WITH_DESCRIPTIONS)) as mock_open:
        fetch_top_headlines(url, count=3)
        fetch_top_headlines(url, count=2)
    assert mock_open.call_count == 2  # different counts → different cache keys
