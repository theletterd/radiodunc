from unittest.mock import patch

from app.news import fetch_random_headline


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
