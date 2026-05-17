from datetime import datetime
from unittest.mock import patch

from app.config import AppConfig, AdBreakPreferences, DJPersona, NewsPreferences, StationConfig
from app.dj_scripts import (
    DEFAULT_AD_PROMPT_TEMPLATE,
    DEFAULT_DJ_PROMPT_TEMPLATE,
    DEFAULT_NEWS_PROMPT_TEMPLATE,
    _build_prompt,
    active_station,
    generate_ad_script,
    generate_news_script,
    pick_active_persona,
)
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


def test_skip_reason_adds_reason_block_to_prompt():
    cfg = _make_config(name="X FM")
    payload = DJScriptGenerateRequest(max_sentences=1, reason="skip")
    prompt = _build_prompt(cfg.station, payload, None, None, cfg)
    assert "audience" in prompt


def test_no_reason_omits_reason_block():
    cfg = _make_config(name="X FM")
    payload = DJScriptGenerateRequest(max_sentences=1)
    prompt = _build_prompt(cfg.station, payload, None, None, cfg)
    assert "pressed Skip" not in prompt


def test_prompt_includes_current_time_and_weekday():
    import re
    cfg = _make_config(name="Time FM")
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=1), None, None, cfg)
    # e.g. "Local time right now: 3:47 PM on Sunday."
    assert "Local time right now:" in prompt
    assert re.search(r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", prompt)


def test_custom_template_can_use_time_placeholders():
    cfg = _make_config(template="It's {current_time} on {current_weekday}.", name="T FM")
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=1), None, None, cfg)
    assert prompt.startswith("It's ")
    assert " on " in prompt


# ── DJ persona / roster scheduling ────────────────────────────────────────────

MONDAY_NOON = datetime(2026, 5, 18, 12, 0)       # weekday() == 0
SATURDAY_22 = datetime(2026, 5, 23, 22, 0)       # weekday() == 5


def test_empty_roster_returns_no_persona():
    station = StationConfig()
    assert pick_active_persona(station, MONDAY_NOON) is None


def test_persona_matches_by_day():
    weekday_dj = DJPersona(name="Weekday Wendy", style="brisk", days=["monday", "tuesday"])
    weekend_dj = DJPersona(name="Weekend Wally", style="loose", days=["saturday", "sunday"])
    station = StationConfig(dj_roster=[weekday_dj, weekend_dj])
    assert pick_active_persona(station, MONDAY_NOON).name == "Weekday Wendy"
    assert pick_active_persona(station, SATURDAY_22).name == "Weekend Wally"


def test_persona_matches_by_hour_range():
    late_dj = DJPersona(name="Late Larry", style="hushed", start_hour=20, end_hour=23)
    day_dj = DJPersona(name="Day Dora", style="bright", start_hour=6, end_hour=19)
    station = StationConfig(dj_roster=[late_dj, day_dj])
    assert pick_active_persona(station, MONDAY_NOON).name == "Day Dora"
    assert pick_active_persona(station, SATURDAY_22).name == "Late Larry"


def test_persona_matches_by_day_and_hour():
    only_sat_night = DJPersona(
        name="Saturday Sam", style="party", days=["saturday"], start_hour=20, end_hour=23
    )
    station = StationConfig(dj_roster=[only_sat_night])
    assert pick_active_persona(station, SATURDAY_22).name == "Saturday Sam"
    assert pick_active_persona(station, MONDAY_NOON) is None


def test_persona_with_wrapping_hour_range():
    overnight = DJPersona(name="Owl", style="mellow", start_hour=22, end_hour=3)
    station = StationConfig(dj_roster=[overnight])
    midnight = datetime(2026, 5, 18, 0, 30)
    early = datetime(2026, 5, 18, 4, 0)
    assert pick_active_persona(station, midnight).name == "Owl"
    assert pick_active_persona(station, early) is None


def test_active_station_overrides_dj_fields():
    persona = DJPersona(
        name="Override Olive",
        style="dramatic",
        voice="echo",
        prompt_template="custom {dj_name}",
    )
    station = StationConfig(
        dj_name="Default Dan",
        dj_style="plain",
        voice="alloy",
        dj_roster=[persona],
    )
    cfg = AppConfig(station=station)
    eff = active_station(station, cfg, now=MONDAY_NOON)
    assert eff.dj_name == "Override Olive"
    assert eff.dj_style == "dramatic"
    assert eff.voice == "echo"
    assert eff.dj_prompt_template == "custom {dj_name}"


def test_active_station_falls_back_when_no_match():
    persona = DJPersona(name="X", style="y", days=["sunday"])
    station = StationConfig(dj_name="Default Dan", dj_roster=[persona])
    cfg = AppConfig(station=station)
    eff = active_station(station, cfg, now=MONDAY_NOON)  # Monday, persona only Sunday
    assert eff.dj_name == "Default Dan"


# ── Ad script generation ──────────────────────────────────────────────────────

def test_generate_ad_script_returns_text_from_openai_call():
    cfg = AppConfig(station=StationConfig(name="Test FM", format="Late Night"))
    with patch("app.dj_scripts._call_openai_text", return_value="Try Acme Beans, the bean for every scene.") as call:
        result = generate_ad_script(cfg.station, cfg)
    assert result == "Try Acme Beans, the bean for every scene."
    sent_prompt = call.call_args[0][0]
    assert "radio sponsor spot" in sent_prompt


def test_generate_ad_script_uses_custom_template():
    cfg = AppConfig(
        station=StationConfig(name="Custom FM", format="Eclectic"),
        alerts={"ads": AdBreakPreferences(prompt_template="AD for {station_name} playing {station_format}")},
    )
    with patch("app.dj_scripts._call_openai_text", return_value="anything") as call:
        generate_ad_script(cfg.station, cfg)
    assert call.call_args[0][0] == "AD for Custom FM playing Eclectic"


def test_generate_ad_script_returns_none_on_openai_failure():
    cfg = AppConfig(station=StationConfig())
    with patch("app.dj_scripts._call_openai_text", return_value=None):
        assert generate_ad_script(cfg.station, cfg) is None


# ── News script generation ───────────────────────────────────────────────────

def _fake_feed(count: int = 3) -> dict:
    items = [
        {"title": f"Headline {i}", "description": f"Description {i}"}
        for i in range(1, count + 1)
    ]
    return {"source": "The Guardian — World", "items": items}


def test_generate_news_script_builds_prompt_and_calls_openai():
    cfg = AppConfig()
    with patch("app.dj_scripts.fetch_top_headlines", return_value=_fake_feed(3)), \
         patch("app.dj_scripts._call_openai_text", return_value="Top story: a thing happened.") as call:
        result = generate_news_script(cfg)
    assert result == "Top story: a thing happened."
    sent_prompt = call.call_args[0][0]
    assert "The Guardian — World" in sent_prompt
    assert "Headline 1" in sent_prompt
    assert "Description 1" in sent_prompt
    assert "Headline 3" in sent_prompt


def test_generate_news_script_returns_none_when_feed_unavailable():
    cfg = AppConfig()
    with patch("app.dj_scripts.fetch_top_headlines", return_value=None):
        assert generate_news_script(cfg) is None


def test_generate_news_script_uses_custom_template():
    cfg = AppConfig(
        alerts={
            "news": NewsPreferences(
                prompt_template="NEWS from {rss_source}: {headlines_block}",
            )
        }
    )
    with patch("app.dj_scripts.fetch_top_headlines", return_value=_fake_feed(2)), \
         patch("app.dj_scripts._call_openai_text", return_value="anything") as call:
        generate_news_script(cfg)
    sent_prompt = call.call_args[0][0]
    assert sent_prompt.startswith("NEWS from The Guardian — World:")
    assert "Headline 1" in sent_prompt


def test_dj_prompt_includes_news_handoff_when_news_break_follows():
    cfg = AppConfig()
    prev = Track(file_path="/m/a.mp3", title="A", artist="X")
    nxt = Track(file_path="/m/b.mp3", title="B", artist="Y")
    prompt = _build_prompt(
        cfg.station,
        DJScriptGenerateRequest(max_sentences=3, news_break_follows=True),
        prev, nxt, cfg,
    )
    assert "news bulletin follows" in prompt
    # Should NOT include the inline headline-weaving instruction.
    assert "work in this real headline" not in prompt
