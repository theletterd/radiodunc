from app.config import AppConfig, StationConfig
from app.dj_scripts import DEFAULT_DJ_PROMPT_TEMPLATE, _build_prompt
from app.models import Track
from app.schemas import DJScriptGenerateRequest


def _make_config(template: str | None = None, **station_kwargs) -> AppConfig:
    station = StationConfig(dj_prompt_template=template, **station_kwargs)
    return AppConfig(station=station)


def test_default_prompt_includes_station_and_track_metadata():
    cfg = _make_config(
        name="Night Drive FM",
        format="Late Night Vinyl",
        dj_name="DJ Verse",
        dj_style="dry and brief",
    )
    prev = Track(file_path="/m/a.mp3", title="Foo", artist="Bar")
    nxt = Track(file_path="/m/b.mp3", title="Baz", artist="Qux")
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=2), prev, nxt, cfg)

    assert "Night Drive FM" in prompt
    assert "DJ Verse" in prompt
    assert "dry and brief" in prompt
    assert "Foo by Bar" in prompt
    assert "Baz by Qux" in prompt
    assert "2-sentence" in prompt


def test_custom_template_overrides_default():
    cfg = _make_config(
        template="STATION={station_name}|DJ={dj_name}|NEXT={next_track}",
        name="KWV 106",
        dj_name="Willy",
    )
    nxt = Track(file_path="/m/x.mp3", title="Track X", artist="Artist Y")
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=3), None, nxt, cfg)

    assert prompt == "STATION=KWV 106|DJ=Willy|NEXT=Track X by Artist Y (filename: x.mp3)"


def test_unknown_placeholder_falls_back_to_default_template():
    cfg = _make_config(
        template="hello {nonexistent_field}",
        name="Fallback FM",
    )
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=1), None, None, cfg)
    assert "Fallback FM" in prompt
    assert "{nonexistent_field}" not in prompt
    assert "Write a 1-sentence" in prompt


def test_era_and_genre_focus_appear_when_set():
    cfg = _make_config(name="Retro", era="1970s", genre_focus=["funk", "soul"])
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=1), None, None, cfg)
    assert "Era: 1970s." in prompt
    assert "Genre focus: funk, soul." in prompt


def test_optional_blocks_appear_when_requested():
    cfg = _make_config(name="X FM")
    payload = DJScriptGenerateRequest(max_sentences=1, include_news=True, include_fake_ad=True)
    prompt = _build_prompt(cfg.station, payload, None, None, cfg)
    assert "News context" in prompt
    assert "Ad context" in prompt
