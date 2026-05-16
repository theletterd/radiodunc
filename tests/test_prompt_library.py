from app.models import Station, Track
from app.prompt_library import (
    render_ad_break_prompt,
    render_news_prompt,
    render_song_transition_prompt,
    render_time_check_prompt,
    render_weather_prompt,
)


def test_prompt_library_placeholders_present():
    station = Station(name="Phase 8 FM", dj_name="DJ Eight")
    previous = Track(file_path="/m/one.mp3", title="One", artist="Artist A")
    next_track = Track(file_path="/m/two.mp3", title="Two", artist="Artist B")

    transition = render_song_transition_prompt(station, previous, next_track, max_sentences=2)
    ad = render_ad_break_prompt(station)
    weather = render_weather_prompt(station, "Austin, TX")
    news = render_news_prompt(station)
    time_check = render_time_check_prompt(station, "8:53am")

    assert "[TRANSITION_PROMPT_PLACEHOLDER]" in transition
    assert "[AD_BREAK_PROMPT_PLACEHOLDER]" in ad
    assert "[WEATHER_PROMPT_PLACEHOLDER]" in weather
    assert "[NEWS_PROMPT_PLACEHOLDER]" in news
    assert "[TIME_CHECK_PROMPT_PLACEHOLDER]" in time_check
