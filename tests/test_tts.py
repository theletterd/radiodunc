import json
import logging
import urllib.error

import pytest

from app.config import AppConfig
from app.tts import (
    OpenAITTSProvider,
    ToneTTSProvider,
    _clip_hash,
    build_tts_provider,
)


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _make_provider() -> OpenAITTSProvider:
    return OpenAITTSProvider(api_key="sk-test", model="gpt-4o-mini-tts", voice="verse")


def test_synthesize_writes_audio_bytes_to_output_path(monkeypatch, tmp_path):
    captured = {}

    def fake_urlopen(request, timeout=40):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"fake-mp3-data")

    monkeypatch.setattr("app.tts.urllib.request.urlopen", fake_urlopen)
    out = tmp_path / "nested" / "clip.mp3"
    _make_provider().synthesize("hello world", "sage", out)

    assert out.read_bytes() == b"fake-mp3-data"
    assert captured["url"] == "https://api.openai.com/v1/audio/speech"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["body"] == {
        "model": "gpt-4o-mini-tts",
        "voice": "sage",
        "input": "hello world",
        "response_format": "mp3",
    }


def test_synthesize_falls_back_to_default_voice_when_blank(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(request, timeout=40):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"x")

    monkeypatch.setattr("app.tts.urllib.request.urlopen", fake_urlopen)
    _make_provider().synthesize("hi", "default", tmp_path / "a.mp3")
    assert seen["body"]["voice"] == "verse"

    _make_provider().synthesize("hi", "", tmp_path / "b.mp3")
    assert seen["body"]["voice"] == "verse"


def test_synthesize_includes_instructions_when_provided(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(request, timeout=40):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"x")

    monkeypatch.setattr("app.tts.urllib.request.urlopen", fake_urlopen)
    _make_provider().synthesize("hi", "sage", tmp_path / "c.mp3", instructions="speak warmly")
    assert seen["body"]["instructions"] == "speak warmly"


def test_synthesize_omits_instructions_when_none(monkeypatch, tmp_path):
    seen = {}

    def fake_urlopen(request, timeout=40):
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(b"x")

    monkeypatch.setattr("app.tts.urllib.request.urlopen", fake_urlopen)
    _make_provider().synthesize("hi", "sage", tmp_path / "d.mp3", instructions=None)
    assert "instructions" not in seen["body"]


def test_synthesize_raises_runtime_error_on_http_error(monkeypatch, tmp_path):
    class _ErrBody:
        def read(self):
            return b"upstream rejected"
        def close(self):
            pass  # Python 3.13's tempfile finalizer calls this during GC.

    def fake_urlopen(request, timeout=40):
        err = urllib.error.HTTPError(
            url=request.full_url, code=401, msg="Unauthorized", hdrs=None, fp=_ErrBody()
        )
        raise err

    monkeypatch.setattr("app.tts.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="OpenAI TTS failed \\(401\\)"):
        _make_provider().synthesize("hi", "sage", tmp_path / "e.mp3")


def test_synthesize_raises_runtime_error_on_network_error(monkeypatch, tmp_path):
    def fake_urlopen(request, timeout=40):
        raise urllib.error.URLError("boom")

    monkeypatch.setattr("app.tts.urllib.request.urlopen", fake_urlopen)
    with pytest.raises(RuntimeError, match="OpenAI TTS network error"):
        _make_provider().synthesize("hi", "sage", tmp_path / "f.mp3")


def test_synthesize_logs_completion_debug_line(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(
        "app.tts.urllib.request.urlopen",
        lambda request, timeout=40: _FakeResponse(b"audio"),
    )
    with caplog.at_level(logging.DEBUG, logger="app.tts"):
        _make_provider().synthesize("hi", "sage", tmp_path / "g.mp3")
    assert any("OpenAI TTS synthesis completed" in rec.message for rec in caplog.records)


def test_build_tts_provider_returns_openai_when_configured():
    config = AppConfig(tts_provider="openai", openai_api_key="sk-test")
    provider = build_tts_provider(config)
    assert isinstance(provider, OpenAITTSProvider)
    assert provider.api_key == "sk-test"


def test_build_tts_provider_raises_when_openai_without_api_key():
    config = AppConfig(tts_provider="openai", openai_api_key=None)
    with pytest.raises(ValueError, match="openai_api_key is required"):
        build_tts_provider(config)


def test_build_tts_provider_returns_tone_for_tone_provider():
    config = AppConfig(tts_provider="tone")
    assert isinstance(build_tts_provider(config), ToneTTSProvider)


def test_clip_hash_is_stable_for_same_inputs():
    assert _clip_hash("script", "verse", "warm") == _clip_hash("script", "verse", "warm")


def test_clip_hash_changes_with_voice():
    assert _clip_hash("script", "verse", "warm") != _clip_hash("script", "sage", "warm")


def test_clip_hash_changes_with_instructions():
    assert _clip_hash("script", "verse", "warm") != _clip_hash("script", "verse", "cold")


def test_clip_hash_treats_none_and_empty_instructions_as_equal():
    assert _clip_hash("script", "verse", None) == _clip_hash("script", "verse", "")
