from datetime import datetime
from unittest.mock import patch

import json

from app.config import (
    AppConfig,
    AdBreakPreferences,
    DJPersona,
    NewsPreferences,
    StationConfig,
    StationIDPreferences,
)
from app.dj_scripts import (
    DEFAULT_AD_PROMPT_TEMPLATE,
    DEFAULT_DJ_PROMPT_TEMPLATE,
    DEFAULT_NEWS_PROMPT_TEMPLATE,
    STATION_ID_VIBES,
    _build_prompt,
    _parse_phrase_lines,
    active_station,
    generate_ad_script,
    generate_news_script,
    get_station_id_phrases,
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
    assert "skipped" in prompt


def test_skip_prompt_bans_stock_well_well_well_opener():
    """The 'well, well, well…' filler showed up repeatedly in production. The
    reason block now lists banned stock openers explicitly so the LLM avoids
    them."""
    cfg = _make_config(name="X FM")
    payload = DJScriptGenerateRequest(max_sentences=1, reason="skip")
    prompt = _build_prompt(cfg.station, payload, None, None, cfg)
    assert "Well, well, well" in prompt
    assert "Alright, alright, alright" in prompt
    assert "AVOID" in prompt or "Don't start with" in prompt


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
    assert eff.personality == "dramatic"
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


def test_generate_ad_script_includes_risque_hint_when_roll_passes(monkeypatch):
    from app.dj_scripts import RISQUE_TONE_HINT
    cfg = AppConfig(
        station=StationConfig(name="Risque FM"),
        alerts={"ads": AdBreakPreferences(risque_chance=1.0)},
    )
    monkeypatch.setattr("random.random", lambda: 0.5)  # 0.5 < 1.0 → risqué
    with patch("app.dj_scripts._call_openai_text", return_value="anything") as call:
        generate_ad_script(cfg.station, cfg)
    assert RISQUE_TONE_HINT in call.call_args[0][0]


def test_generate_ad_script_omits_risque_hint_when_roll_fails(monkeypatch):
    from app.dj_scripts import RISQUE_TONE_HINT
    cfg = AppConfig(
        station=StationConfig(name="Tame FM"),
        alerts={"ads": AdBreakPreferences(risque_chance=0.0)},
    )
    monkeypatch.setattr("random.random", lambda: 0.5)  # 0.5 < 0.0 is false → tame
    with patch("app.dj_scripts._call_openai_text", return_value="anything") as call:
        generate_ad_script(cfg.station, cfg)
    assert RISQUE_TONE_HINT not in call.call_args[0][0]


def test_generate_ad_script_injects_random_category_from_pool(monkeypatch):
    """Each call picks one category from AD_CATEGORIES so the LLM stays focused
    rather than anchoring on whatever's last in an embedded list."""
    from app.dj_scripts import AD_CATEGORIES
    cfg = AppConfig(station=StationConfig(name="Cat FM", format="Eclectic"))
    # Force the random pick to a known value so the assertion is deterministic.
    monkeypatch.setattr("random.choice", lambda seq: seq[2])
    with patch("app.dj_scripts._call_openai_text", return_value="anything") as call:
        generate_ad_script(cfg.station, cfg)
    sent_prompt = call.call_args[0][0]
    assert AD_CATEGORIES[2] in sent_prompt


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
        result = generate_news_script(cfg, newsreader_name="Alex Morgan")
    assert result == "Top story: a thing happened."
    sent_prompt = call.call_args[0][0]
    assert "The Guardian — World" in sent_prompt
    assert "Headline 1" in sent_prompt
    assert "Description 1" in sent_prompt
    assert "Headline 3" in sent_prompt
    assert "Alex Morgan" in sent_prompt
    # Prompt should describe intro/outro structure
    assert "INTRO" in sent_prompt
    assert "OUTRO" in sent_prompt


def test_generate_news_script_falls_back_to_generic_name():
    cfg = AppConfig()
    with patch("app.dj_scripts.fetch_top_headlines", return_value=_fake_feed(2)), \
         patch("app.dj_scripts._call_openai_text", return_value="bulletin") as call:
        generate_news_script(cfg)  # no name passed
    sent_prompt = call.call_args[0][0]
    assert "your newsreader" in sent_prompt


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
        generate_news_script(cfg, newsreader_name="Alex Morgan")
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


# ── Station ID phrase generation ──────────────────────────────────────────────

def _station_id_config(name: str = "Test FM", phrase_count: int = 10) -> AppConfig:
    return AppConfig(
        station=StationConfig(name=name, tagline="The Beat"),
        alerts={"station_id": StationIDPreferences(phrase_count=phrase_count)},
    )


def test_parse_phrase_lines_strips_numbered_prefixes():
    response = "1. Foo bar\n2. Baz qux\n10. Ten ten ten"
    assert _parse_phrase_lines(response) == ["Foo bar", "Baz qux", "Ten ten ten"]


def test_parse_phrase_lines_strips_bullet_prefixes():
    response = "- Dash phrase\n• Bullet phrase\n* Star phrase"
    assert _parse_phrase_lines(response) == ["Dash phrase", "Bullet phrase", "Star phrase"]


def test_parse_phrase_lines_strips_surrounding_quotes():
    response = '"Foo bar baz"\n\'Quoted phrase\''
    assert _parse_phrase_lines(response) == ["Foo bar baz", "Quoted phrase"]


def test_parse_phrase_lines_filters_by_length():
    too_short = "ab"  # len 2 → out
    just_long_enough = "abcd"  # len 4 → in (3 < len < 200)
    too_long = "x" * 200  # len 200 → out
    just_under_max = "y" * 199  # len 199 → in
    response = "\n".join([too_short, just_long_enough, too_long, just_under_max])
    out = _parse_phrase_lines(response)
    assert just_long_enough in out
    assert just_under_max in out
    assert too_short not in out
    assert too_long not in out


def test_parse_phrase_lines_handles_none_and_empty():
    assert _parse_phrase_lines(None) == []
    assert _parse_phrase_lines("") == []
    assert _parse_phrase_lines("   \n  \n") == []


def test_get_station_id_phrases_generates_when_cache_empty(monkeypatch, tmp_path):
    cache_file = tmp_path / "phrases.json"
    monkeypatch.setattr("app.dj_scripts.STATION_ID_CACHE_FILE", cache_file)

    calls: list[str] = []

    def fake_batch(vibe_name, vibe_instruction, count, station_name, tagline, config):
        calls.append(vibe_name)
        return [f"{vibe_name} phrase one", f"{vibe_name} phrase two"]

    monkeypatch.setattr("app.dj_scripts._generate_station_id_batch", fake_batch)

    cfg = _station_id_config(name="Cache FM", phrase_count=10)
    result = get_station_id_phrases(cfg)

    # One call per vibe
    assert sorted(calls) == sorted(name for name, _ in STATION_ID_VIBES)
    assert len(calls) == 5
    # Two phrases per vibe, all unique → 10 entries
    assert len(result) == 10
    # Cache file written, keyed by station name
    assert cache_file.exists()
    saved = json.loads(cache_file.read_text())
    assert "Cache FM" in saved
    assert saved["Cache FM"] == result


def test_get_station_id_phrases_returns_cached_without_llm_call(monkeypatch, tmp_path):
    cache_file = tmp_path / "phrases.json"
    cache_file.write_text(json.dumps({"Cached FM": ["already here", "second one"]}))
    monkeypatch.setattr("app.dj_scripts.STATION_ID_CACHE_FILE", cache_file)

    def boom(*args, **kwargs):
        raise AssertionError("LLM should not be called when cache hit")

    monkeypatch.setattr("app.dj_scripts._generate_station_id_batch", boom)

    cfg = _station_id_config(name="Cached FM")
    assert get_station_id_phrases(cfg) == ["already here", "second one"]


def test_get_station_id_phrases_keeps_old_station_when_name_changes(monkeypatch, tmp_path):
    cache_file = tmp_path / "phrases.json"
    cache_file.write_text(json.dumps({"Old FM": ["old one", "old two"]}))
    monkeypatch.setattr("app.dj_scripts.STATION_ID_CACHE_FILE", cache_file)

    def fake_batch(vibe_name, vibe_instruction, count, station_name, tagline, config):
        return [f"new for {station_name}"]

    monkeypatch.setattr("app.dj_scripts._generate_station_id_batch", fake_batch)

    cfg = _station_id_config(name="New FM", phrase_count=5)
    result = get_station_id_phrases(cfg)
    assert all("New FM" in p for p in result)

    saved = json.loads(cache_file.read_text())
    assert "Old FM" in saved
    assert "New FM" in saved
    assert saved["Old FM"] == ["old one", "old two"]


def test_get_station_id_phrases_dedupes_case_insensitively(monkeypatch, tmp_path):
    cache_file = tmp_path / "phrases.json"
    monkeypatch.setattr("app.dj_scripts.STATION_ID_CACHE_FILE", cache_file)

    # Each vibe returns the same effective phrase modulo casing/punctuation.
    variants = iter([
        ["This is X"],
        ["this is x."],
        ["THIS IS X!"],
        ["  this is x ,"],
        ["This is X?"],
    ])

    def fake_batch(vibe_name, vibe_instruction, count, station_name, tagline, config):
        return next(variants)

    monkeypatch.setattr("app.dj_scripts._generate_station_id_batch", fake_batch)

    cfg = _station_id_config(name="Dedupe FM", phrase_count=5)
    result = get_station_id_phrases(cfg)
    assert len(result) == 1
    # First-seen casing/form is preserved
    assert result[0] == "This is X"


def test_get_station_id_phrases_falls_back_when_all_batches_empty(monkeypatch, tmp_path):
    cache_file = tmp_path / "phrases.json"
    monkeypatch.setattr("app.dj_scripts.STATION_ID_CACHE_FILE", cache_file)

    monkeypatch.setattr(
        "app.dj_scripts._generate_station_id_batch",
        lambda *args, **kwargs: [],
    )

    cfg = _station_id_config(name="Empty FM", phrase_count=5)
    result = get_station_id_phrases(cfg)
    assert result == ["This is Empty FM."]
    saved = json.loads(cache_file.read_text())
    assert saved["Empty FM"] == ["This is Empty FM."]


def test_get_station_id_phrases_distributes_phrase_count_per_vibe(monkeypatch, tmp_path):
    cache_file = tmp_path / "phrases.json"
    monkeypatch.setattr("app.dj_scripts.STATION_ID_CACHE_FILE", cache_file)

    seen_counts: list[int] = []

    def fake_batch(vibe_name, vibe_instruction, count, station_name, tagline, config):
        seen_counts.append(count)
        return [f"{vibe_name}-{i}" for i in range(count)]

    monkeypatch.setattr("app.dj_scripts._generate_station_id_batch", fake_batch)

    cfg = _station_id_config(name="Split FM", phrase_count=15)
    get_station_id_phrases(cfg)

    # ceil(15/5) == 3, every vibe asked for 3
    assert seen_counts == [3, 3, 3, 3, 3]


# ── DJ shifts (new per-day-hours model) ──────────────────────────────────────

def test_legacy_persona_shape_migrates_to_shifts():
    """Old-shape {days:[...], start_hour, end_hour} auto-migrates on load."""
    from app.config import DJShift
    p = DJPersona(name="Legacy Lou", style="loose", days=["friday", "saturday"], start_hour=20, end_hour=23)
    assert len(p.shifts) == 2
    assert p.shifts[0] == DJShift(day="friday", start_hour=20, end_hour=23)
    assert p.shifts[1] == DJShift(day="saturday", start_hour=20, end_hour=23)


def test_legacy_days_only_migrates_to_full_day_shifts():
    p = DJPersona(name="All-day", style="constant", days=["sunday"])
    assert p.shifts == [DJShift_for("sunday", 0, 23)]


def test_legacy_hours_only_migrates_to_every_day_shifts():
    p = DJPersona(name="Late everyday", style="moody", start_hour=22, end_hour=2)
    # Should produce 7 shifts, one per weekday, with the same hours.
    assert len(p.shifts) == 7
    assert {s.day for s in p.shifts} == set(["monday","tuesday","wednesday","thursday","friday","saturday","sunday"])
    assert all(s.start_hour == 22 and s.end_hour == 2 for s in p.shifts)


def test_persona_with_no_schedule_keeps_empty_shifts():
    p = DJPersona(name="Always on", style="floating")
    assert p.shifts == []


def test_persona_with_per_day_hour_shifts():
    """The new shape lets a single persona have different hours on different days."""
    from app.config import DJShift
    p = DJPersona(
        name="Variable Vince", style="flexible",
        shifts=[
            DJShift(day="friday", start_hour=20, end_hour=23),
            DJShift(day="saturday", start_hour=19, end_hour=1),  # wraps past midnight
        ],
    )
    fri_2200 = datetime(2026, 5, 22, 22, 0)  # Friday
    sat_2300 = datetime(2026, 5, 23, 23, 0)  # Saturday
    sat_0030 = datetime(2026, 5, 23, 0, 30)  # Saturday (early morning)
    sat_0200 = datetime(2026, 5, 23, 2, 0)   # Saturday — outside Sat shift
    from app.dj_scripts import _persona_matches
    assert _persona_matches(p, fri_2200) is True
    assert _persona_matches(p, sat_2300) is True
    assert _persona_matches(p, sat_0030) is True  # wrap-around
    assert _persona_matches(p, sat_0200) is False


def DJShift_for(day, start, end):
    """Tiny helper to keep the legacy migration tests readable."""
    from app.config import DJShift
    return DJShift(day=day, start_hour=start, end_hour=end)


def test_shift_rejects_invalid_day():
    import pytest as _pytest
    from app.config import DJShift
    with _pytest.raises(Exception):
        DJShift(day="funday", start_hour=10, end_hour=12)


def test_legacy_and_new_shape_in_same_persona_prefers_new():
    """If both 'shifts' and 'days/start/end' are provided, shifts wins; legacy keys
    are dropped silently (extra='forbid' would have raised otherwise)."""
    from app.config import DJShift
    p = DJPersona(
        name="Mixed", style="confused",
        shifts=[DJShift(day="monday", start_hour=10, end_hour=11)],
        days=["sunday"], start_hour=22, end_hour=23,
    )
    assert p.shifts == [DJShift(day="monday", start_hour=10, end_hour=11)]


# ── Persona refactor: dj_style → personality migration ──────────────────────

def test_station_config_legacy_dj_style_migrates_to_personality():
    """Old configs (and tests) pass dj_style — migration validator renames it."""
    s = StationConfig(dj_style="warm and dry")
    assert s.personality == "warm and dry"
    assert not hasattr(s, "dj_style")


def test_dj_persona_legacy_style_migrates_to_personality():
    p = DJPersona(name="Legacy", style="loose")
    assert p.personality == "loose"


def test_personality_field_takes_precedence_over_legacy_style():
    """If both are set, the new field wins and the old is dropped."""
    p = DJPersona(name="Mix", personality="bright", style="should-be-ignored")
    assert p.personality == "bright"


def test_dj_prompt_template_dj_style_alias_still_resolves():
    """Custom templates written with {dj_style} continue to render even though
    the canonical field is now {personality}."""
    cfg = AppConfig(
        station=StationConfig(
            personality="loose and warm",
            dj_prompt_template="DJ vibe: {dj_style}",
        ),
    )
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=1), None, None, cfg)
    assert prompt == "DJ vibe: loose and warm"


def test_dj_prompt_template_new_personality_placeholder_works():
    cfg = AppConfig(
        station=StationConfig(
            personality="dry",
            dj_prompt_template="vibe: {personality}",
        ),
    )
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=1), None, None, cfg)
    assert prompt == "vibe: dry"


# ── OpenAI temperature plumbing ─────────────────────────────────────────────

def test_call_openai_text_uses_config_temperature_by_default(monkeypatch):
    """Verifies the configured openai_text_temperature lands in the request body."""
    from app.dj_scripts import _call_openai_text
    captured = {}

    class FakeResp:
        def read(self): return b'{"output_text": "hi"}'
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def fake_urlopen(req, timeout=40):
        body = json.loads(req.data.decode('utf-8'))
        captured.update(body)
        return FakeResp()

    monkeypatch.setattr("app.dj_scripts.urllib.request.urlopen", fake_urlopen)
    cfg = AppConfig(
        script_provider="openai",
        openai_api_key="sk-fake",
        openai_text_temperature=1.4,
    )
    _call_openai_text("test prompt", cfg)
    assert captured["temperature"] == 1.4


def test_call_openai_text_temperature_override_wins(monkeypatch):
    """Per-call temperature overrides the config default — used by news for
    a more professional / predictable tone."""
    from app.dj_scripts import _call_openai_text
    captured = {}

    class FakeResp:
        def read(self): return b'{"output_text": "ok"}'
        def __enter__(self): return self
        def __exit__(self, *_): return False

    def fake_urlopen(req, timeout=40):
        body = json.loads(req.data.decode('utf-8'))
        captured.update(body)
        return FakeResp()

    monkeypatch.setattr("app.dj_scripts.urllib.request.urlopen", fake_urlopen)
    cfg = AppConfig(
        script_provider="openai",
        openai_api_key="sk-fake",
        openai_text_temperature=1.2,
    )
    _call_openai_text("test prompt", cfg, temperature=0.5)
    assert captured["temperature"] == 0.5


def test_generate_news_script_uses_lower_temperature(monkeypatch):
    """The news pathway pulls temperature down to 0.7 for professional tone,
    regardless of the higher config default used for DJ banter."""
    from app.dj_scripts import generate_news_script

    captured_temps = []

    def fake_call(prompt, config, *, temperature=None):
        captured_temps.append(temperature)
        return "bulletin"

    monkeypatch.setattr("app.dj_scripts._call_openai_text", fake_call)
    monkeypatch.setattr(
        "app.dj_scripts.fetch_top_headlines",
        lambda url, count: {"source": "Source", "items": [{"title": "t", "description": "d"}]},
    )

    cfg = AppConfig(openai_text_temperature=1.5)  # high default
    generate_news_script(cfg, newsreader_name="Alex")
    assert captured_temps == [0.7]


# ── Station name placeholder substitution ───────────────────────────────────

def test_substitute_station_placeholder_uses_spoken_name_when_set():
    from app.dj_scripts import substitute_station_placeholder
    cfg = AppConfig(station=StationConfig(
        name="RadioDunc 107.2 FM",
        spoken_name="Radio Dunk, one oh seven point two F M",
    ))
    result = substitute_station_placeholder(
        "You're listening to [[STATION]]. We'll be right back.",
        cfg,
    )
    assert result == "You're listening to Radio Dunk, one oh seven point two F M. We'll be right back."


def test_substitute_station_placeholder_falls_back_to_name_when_spoken_unset():
    from app.dj_scripts import substitute_station_placeholder
    cfg = AppConfig(station=StationConfig(name="KVW 96", spoken_name=None))
    result = substitute_station_placeholder("This is [[STATION]] tonight.", cfg)
    assert result == "This is KVW 96 tonight."


def test_substitute_station_placeholder_passthrough_when_no_placeholder():
    from app.dj_scripts import substitute_station_placeholder
    cfg = AppConfig(station=StationConfig(name="X FM"))
    text = "No placeholder here, just text."
    assert substitute_station_placeholder(text, cfg) == text


def test_substitute_station_placeholder_handles_multiple_occurrences():
    from app.dj_scripts import substitute_station_placeholder
    cfg = AppConfig(station=StationConfig(name="KWV", spoken_name="K-W-V"))
    result = substitute_station_placeholder(
        "[[STATION]] always. You're on [[STATION]].",
        cfg,
    )
    assert result == "K-W-V always. You're on K-W-V."


def test_substitute_station_placeholder_handles_empty_or_none():
    from app.dj_scripts import substitute_station_placeholder
    cfg = AppConfig()
    assert substitute_station_placeholder("", cfg) == ""
    assert substitute_station_placeholder(None, cfg) is None


def test_dj_prompt_tells_llm_to_emit_station_placeholder():
    """Regression hook: the DJ prompt must instruct the LLM to emit [[STATION]]
    rather than spelling the name out itself, since otherwise we lose the
    pronunciation control."""
    cfg = _make_config(name="Test 96")
    prompt = _build_prompt(cfg.station, DJScriptGenerateRequest(max_sentences=1), None, None, cfg)
    assert "[[STATION]]" in prompt


def test_station_id_prompt_requires_placeholder():
    """Station-ID stingers are MEANT to name the station, so the placeholder
    instruction is even more emphatic here ('every line must contain')."""
    from app.dj_scripts import STATION_ID_PROMPT_TEMPLATE
    assert "[[STATION]]" in STATION_ID_PROMPT_TEMPLATE


def test_generate_ad_script_substitutes_station_placeholder(monkeypatch):
    """Ads can reference the station ('brought to you by [[STATION]]'). The
    substitution kicks in on the LLM's returned text."""
    cfg = AppConfig(station=StationConfig(name="KWV", spoken_name="K Double-You V"))
    with patch("app.dj_scripts._call_openai_text",
               return_value="Brought to you by [[STATION]]. The bean for every scene."):
        result = generate_ad_script(cfg.station, cfg)
    assert "K Double-You V" in result
    assert "[[STATION]]" not in result
