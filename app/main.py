import json
import logging
import os
import random
import threading
import time
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy import func

from . import migrations, news_cache, prefetch, single_worker
from .config import AppConfig, StationConfig, load_config, save_config
from .database import Base, SessionLocal, engine, get_db
from .logging_setup import configure_logging, log_event as _log_event
from .models import DJClip, PlayerState, Track
from .news_cache import NEWS_BLOCK_ON_MISS_S, get_news_clip, wait_for_fresh_news
from .dj_scripts import (
    DJ_AVATAR_DIR,
    active_dj,
    active_station,
    generate_ad_script,
    generate_dj_avatar,
    generate_dj_script,
    get_station_id_phrases,
)
from .scanner import scan_library
from .schemas import (
    LibraryScanRequest,
    QueueInjectRequest,
    QueueInjectResponse,
    PlayerStateResponse,
    PlayerStateUpdateRequest,
    PlayerPlayRequest,
    PlayerActionResponse,
    PlayerNextRequest,
    DJScriptGenerateRequest,
    StingerUrlResponse,
    TTSPreviewRequest,
    TTSPreviewResponse,
    StationOut,
    TrackOut,
    PlayerNextResponse,
    QueueItemOut,
    QueuePreviewResponse,
    QueueReorderRequest,
    LibraryStatusResponse,
    QueueExtendRequest,
    QueueExtendResponse,
)
from .scheduler import build_station_queue
from .tts import build_tts_provider, get_or_create_dj_clip

Base.metadata.create_all(bind=engine)

migrations.run_all()

logger = logging.getLogger(__name__)

configure_logging()

# After configure_logging so the CRITICAL actually reaches a handler.
single_worker.ensure_single_worker()

app = FastAPI(title="RadioDunc", version="0.3.0")
app.mount("/ui", StaticFiles(directory="app/ui", html=True), name="ui")


@app.middleware("http")
async def log_requests(request, call_next):
    started = time.perf_counter()
    _log_event("request.start", level=logging.DEBUG, method=request.method, path=request.url.path, client=request.client.host if request.client else "unknown")
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.exception("request.error method=%s path=%s elapsed_ms=%s", request.method, request.url.path, elapsed_ms)
        raise
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    _log_event("request.end", level=logging.DEBUG, method=request.method, path=request.url.path, status=response.status_code, elapsed_ms=elapsed_ms)
    return response


@app.get("/health")
def healthcheck():
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/ui/")


@app.get("/config", response_model=AppConfig)
def get_config():
    return load_config()


# Config fields whose values feed into a *generated* DJ/news/ad/stinger clip
# (script wording, TTS voice, model, key). When any of these changes, every
# in-memory artefact built against the old values is stale and must be
# rebuilt. Used by _on_config_changed below.
_GENERATION_TOPLEVEL_FIELDS = (
    "tts_provider",
    "script_provider",
    "openai_text_model",
    "openai_text_temperature",
    "openai_tts_model",
    "openai_tts_voice",
)


def _on_config_changed(old: AppConfig, new: AppConfig) -> None:
    """Invalidate caches whose contents derive from config.

    Called from update_config AFTER save_config succeeds. Comparisons are done
    on the dumped pydantic dicts so nested submodels diff cleanly. Each cache
    is keyed off the narrowest possible field set so tweaking risque_chance
    doesn't drop the news bulletin etc.

    Only the in-memory caches that bake config values into their contents are
    handled here — disk-backed caches (station-ID stinger phrases) and
    TTL-bounded caches (weather summary, 30 min) are intentionally left to
    expire naturally, since the cost of an extra LLM/HTTP round-trip is
    small and the eviction logic isn't worth the surface area.

    Failures here are logged and swallowed — a botched cache flush must NOT
    make the PUT /config call fail (the new config is already on disk).
    """
    try:
        old_d = old.model_dump()
        new_d = new.model_dump()
        old_station = old_d.get("station", {})
        new_station = new_d.get("station", {})
        old_alerts = old_d.get("alerts", {})
        new_alerts = new_d.get("alerts", {})

        generation_changed = any(
            old_d.get(f) != new_d.get(f) for f in _GENERATION_TOPLEVEL_FIELDS
        )

        # ── DJ-clip prefetch ─────────────────────────────────────────────
        # The prefetched clip was synthesised against the OLD station persona,
        # cadence settings, and TTS voice. Any change to station, alerts, or
        # generation knobs makes it stale.
        prefetch_inputs_changed = (
            generation_changed
            or old_station != new_station
            or old_alerts != new_alerts
        )
        if prefetch_inputs_changed:
            prefetch.clear()
            _log_event("config.cache.invalidated", cache="prefetch")

        # ── News bulletin cache ──────────────────────────────────────────
        # Renaming the station can leave the cached bulletin saying "the OLD
        # station name" in its intro/outro. Newsreader voices, RSS source,
        # headline count, prompt template, and any text/TTS generation knob
        # all change what the next bulletin sounds like.
        news_relevant_old = {
            "name": old_station.get("name"),
            "spoken_name": old_station.get("spoken_name"),
            "news": old_alerts.get("news"),
        }
        news_relevant_new = {
            "name": new_station.get("name"),
            "spoken_name": new_station.get("spoken_name"),
            "news": new_alerts.get("news"),
        }
        if generation_changed or news_relevant_old != news_relevant_new:
            news_cache.invalidate()
            _log_event("config.cache.invalidated", cache="news")
    except Exception:  # noqa: BLE001
        logger.exception("Config-change cache invalidation hook raised; ignoring")


@app.put("/config", response_model=AppConfig)
def update_config(config: AppConfig):
    old_config = load_config()
    save_config(config)
    _on_config_changed(old_config, config)
    return config


@app.get("/station", response_model=StationOut)
def get_station():
    config = load_config()
    return _station_out(active_station(config.station, config))


def _track_label(track: Track) -> str:
    """Display label for a track. Falls back to filename stem when metadata is missing."""
    if track.artist and track.title:
        return f"{track.artist} - {track.title}"
    if track.artist or track.title:
        return f"{track.artist or 'Unknown'} - {track.title or 'Untitled'}"
    if track.file_path:
        return Path(track.file_path).stem
    return f"Track {track.id}"


def _station_out(station: StationConfig, *, active_dj_id: str | None = None) -> StationOut:
    return StationOut(
        name=station.name,
        tagline=station.tagline,
        format=station.format,
        description=station.description,
        era=station.era,
        genre_focus=list(station.genre_focus),
        dj_name=station.dj_name,
        personality=station.personality,
        active_dj_id=active_dj_id,
    )


@app.post("/library/scan")
def scan_library_endpoint(payload: LibraryScanRequest, db: Session = Depends(get_db)):
    config = load_config()
    _log_event("library.scan.requested", requested_folder=payload.folder_path or "<config-default>")
    target_folder = payload.folder_path or config.music_folder
    try:
        result = scan_library(target_folder, db)
        _log_event("library.scan.completed", folder=target_folder, total_tracks=result.get("total_tracks"), new_tracks=result.get("new_tracks"))
        return {"folder_path": target_folder, **result}
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to scan library: {exc}") from exc


@app.get("/library/status", response_model=LibraryStatusResponse)
def library_status(db: Session = Depends(get_db)):
    track_count = db.query(Track).count()
    last_scan_dt = db.query(func.max(Track.created_at)).scalar()
    last_scan_at = last_scan_dt.isoformat() if last_scan_dt is not None else None
    return LibraryStatusResponse(track_count=track_count, last_scan_at=last_scan_at)


@app.get("/tracks", response_model=list[TrackOut])
def list_tracks(db: Session = Depends(get_db)):
    return db.query(Track).order_by(Track.artist.asc(), Track.album.asc(), Track.title.asc()).all()


@app.get("/library/search", response_model=list[TrackOut])
def search_library(q: str = "", db: Session = Depends(get_db)):
    if not q.strip():
        return []
    pattern = f"%{q}%"
    return (
        db.query(Track)
        .filter(Track.title.ilike(pattern) | Track.artist.ilike(pattern))
        .order_by(Track.artist.asc(), Track.title.asc())
        .limit(10)
        .all()
    )


@app.post("/player/queue/inject", response_model=QueueInjectResponse)
def queue_inject(payload: QueueInjectRequest, db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == payload.track_id).first()
    if track is None:
        raise HTTPException(status_code=404, detail=f"Track {payload.track_id} not found")

    state = db.query(PlayerState).order_by(PlayerState.id.asc()).first()
    if state is None or not state.queue_json:
        raise HTTPException(status_code=400, detail="No active player queue")

    queue = json.loads(state.queue_json)
    if not queue:
        raise HTTPException(status_code=400, detail="Queue is empty")

    label = _track_label(track)
    # `requested: True` regardless of position — both "next" and "end" are
    # caller-initiated additions; when the queue eventually advances to the
    # track, the DJ banter should mention it was a request.
    item = {"type": "track", "track_id": track.id, "label": label, "requested": True}
    if payload.position == "end":
        insert_at = len(queue)
    else:
        insert_at = state.queue_index + 1
    queue.insert(insert_at, item)
    state.queue_json = json.dumps(queue)
    db.commit()

    # The prefetch cache holds a clip for the *next* track. A "next"-position
    # insert displaces what was queued at idx+1 — the prefetched clip is now
    # stale (it was generated for what's now at idx+2). An "end"-position
    # insert leaves the immediate next-track untouched, so the prefetch stays
    # valid and the user doesn't pay for a re-synthesis they don't need.
    if payload.position == "next":
        prefetch.clear()

    _log_event(
        "queue.inject", level=logging.DEBUG,
        track_id=track.id, position=insert_at, queue_depth=len(queue),
        placement=payload.position,
    )
    return QueueInjectResponse(position=insert_at, label=label, queue_depth=len(queue))


def _get_or_create_player_state(db: Session) -> PlayerState:
    # Known race, deliberately unguarded: every queue mutation does
    # json.loads(state.queue_json) → modify → json.dumps → commit, so two
    # overlapping requests (e.g. queue_inject racing player_next) can lose
    # an update — last write wins, silently. The single-user UI makes
    # overlap rare and the blast radius is one queued track vanishing, so
    # a comment is the right amount of fix. If it's ever actually observed,
    # the remedy is optimistic versioning (a version column checked at
    # commit), not a lock.
    state = db.query(PlayerState).order_by(PlayerState.id.asc()).first()
    if state:
        return state
    state = PlayerState()
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


def _build_player_state_response(db: Session, state: PlayerState) -> PlayerStateResponse:
    config = load_config()
    queue = json.loads(state.queue_json) if state.queue_json else []
    now = queue[state.queue_index] if queue and 0 <= state.queue_index < len(queue) else None
    current_track = None
    if state.current_track_id is not None:
        current_track = db.query(Track).filter(Track.id == state.current_track_id).first()

    # The on-air avatar lives on the client side; it builds the URL from
    # the DJ's id. active_station() flattens the override into dj_name etc.
    # but loses the id, so we plumb it through separately.
    on_air_dj = active_dj(config.station, config)
    return PlayerStateResponse(
        is_playing=state.is_playing,
        volume=state.volume,
        station=_station_out(
            active_station(config.station, config),
            active_dj_id=on_air_dj.id if on_air_dj else None,
        ),
        current_track=current_track,
        queue_depth=len(queue),
        queue_position=state.queue_index,
        now_playing_type=now.get("type") if now else None,
        now_playing_label=now.get("label") if now else None,
        last_error=state.last_error,
    )


@app.get("/player/status", response_model=PlayerStateResponse)
def player_status(db: Session = Depends(get_db)):
    return _build_player_state_response(db, _get_or_create_player_state(db))


@app.get("/player/queue", response_model=QueuePreviewResponse)
def player_queue(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    queue = json.loads(state.queue_json) if state.queue_json else []
    current_pos = state.queue_index

    upcoming = [
        (i, queue[i])
        for i in range(current_pos + 1, len(queue))
        if queue[i].get("type") == "track" and queue[i].get("track_id") is not None
    ]

    # Fetch metadata for the whole upcoming window in one query rather than
    # per item — the queue is routinely 30+ deep and grows with "Add more".
    # Tracks deleted from the library since being queued simply come back
    # without metadata rather than dropping out of the list.
    track_ids = {item["track_id"] for _, item in upcoming}
    tracks_by_id: dict[int, Track] = {}
    if track_ids:
        tracks_by_id = {
            t.id: t for t in db.query(Track).filter(Track.id.in_(track_ids)).all()
        }

    upcoming_items: list[QueueItemOut] = []
    for i, item in upcoming:
        track = tracks_by_id.get(item["track_id"])
        upcoming_items.append(
            QueueItemOut(
                position=i,
                track_id=item["track_id"],
                label=item.get("label", f"Track {item['track_id']}"),
                file_path=track.file_path if track else None,
                title=track.title if track else None,
                artist=track.artist if track else None,
                album=track.album if track else None,
                year=track.year if track else None,
                genre=track.genre if track else None,
                duration_seconds=track.duration_seconds if track else None,
                bitrate=track.bitrate if track else None,
            )
        )
    return QueuePreviewResponse(
        items=upcoming_items,
        queue_position=current_pos,
        queue_depth=len(queue),
    )


@app.delete("/player/queue/{position}", status_code=204)
def delete_queue_item(
    position: int,
    db: Session = Depends(get_db),
):
    state = _get_or_create_player_state(db)
    queue = json.loads(state.queue_json) if state.queue_json else []
    if position <= state.queue_index or position >= len(queue):
        raise HTTPException(
            status_code=404,
            detail="Position out of range or refers to current/past track",
        )
    queue.pop(position)
    state.queue_json = json.dumps(queue)
    db.commit()
    prefetch.clear()


@app.post("/player/queue/reorder", status_code=204)
def reorder_queue_item(payload: QueueReorderRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    queue = json.loads(state.queue_json) if state.queue_json else []
    current = state.queue_index
    for pos in (payload.from_position, payload.to_position):
        if pos <= current or pos >= len(queue):
            raise HTTPException(status_code=400, detail=f"Position {pos} is out of reorderable range")
    if payload.from_position == payload.to_position:
        return
    item = queue.pop(payload.from_position)
    queue.insert(payload.to_position, item)
    state.queue_json = json.dumps(queue)
    db.commit()
    prefetch.clear()
    _log_event("queue.reorder", level=logging.DEBUG, from_position=payload.from_position, to_position=payload.to_position)


@app.post("/player/queue/extend", response_model=QueueExtendResponse)
def queue_extend(payload: QueueExtendRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    if not state.queue_json:
        raise HTTPException(status_code=400, detail="No active player queue")

    queue = json.loads(state.queue_json)
    config = load_config()

    already_queued = {item["track_id"] for item in queue if item.get("track_id")}

    try:
        candidates = build_station_queue(db, config, size=payload.count * 3, seed=None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    new_tracks = [t for t in candidates["tracks"] if t.id not in already_queued][: payload.count]

    new_items = [{"type": "track", "track_id": t.id, "label": _track_label(t)} for t in new_tracks]
    queue.extend(new_items)
    state.queue_json = json.dumps(queue)
    db.commit()

    _log_event("queue.extend", level=logging.DEBUG, added=len(new_items), queue_depth=len(queue))
    return QueueExtendResponse(added=len(new_items), queue_depth=len(queue))


def _safe_media_path(raw_path: str, config: AppConfig) -> Path:
    media_path = Path(raw_path).expanduser().resolve()
    allowed_roots = [Path(config.music_folder).expanduser().resolve(), Path("generated").resolve()]
    if not any(str(media_path).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Media path is outside allowed roots")
    if not media_path.exists() or not media_path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return media_path


_AUDIO_MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}

@app.get("/media/track/{track_id}")
def media_track(track_id: int, db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if track is None:
        raise HTTPException(status_code=404, detail="Track not found")
    config = load_config()
    media_path = _safe_media_path(track.file_path, config)
    try:
        data = media_path.read_bytes()
    except OSError as exc:
        logger.warning("Track temporarily unavailable path=%s err=%s", media_path, exc)
        raise HTTPException(status_code=503, detail="Track temporarily unavailable") from exc
    media_type = _AUDIO_MEDIA_TYPES.get(media_path.suffix.lower(), "application/octet-stream")
    return Response(content=data, media_type=media_type)


@app.get("/media/dj-clip/{clip_hash}")
def media_dj_clip(clip_hash: str, db: Session = Depends(get_db)):
    clip = db.query(DJClip).filter(DJClip.script_hash == clip_hash).first()
    if clip is None:
        raise HTTPException(status_code=404, detail="DJ clip not found")
    config = load_config()
    media_path = _safe_media_path(clip.audio_path, config)
    media_type = _AUDIO_MEDIA_TYPES.get(media_path.suffix.lower(), "application/octet-stream")
    return FileResponse(str(media_path), media_type=media_type, filename=media_path.name)


# ── DJ avatars ──────────────────────────────────────────────────────────────
# Stylised portrait images per DJ. Generation is manual (button in the DJ
# editor) so cost is explicit and predictable — see generate_dj_avatar in
# dj_scripts.py for the model details. Files live in generated/dj_icons
# keyed by the DJ's UUID so renames don't strand the image.

@app.post("/djs/{dj_id}/avatar")
def generate_dj_avatar_endpoint(dj_id: str):
    """Generate (or regenerate) the avatar for a DJ.

    Synchronous — the call typically returns in 5–15 s. Front-end shows a
    "Generating…" status while the request is in flight. Returns the new
    avatar URL plus a server-side timestamp so the caller can cache-bust
    the <img> src.
    """
    config = load_config()
    dj = next((d for d in config.station.djs if d.id == dj_id), None)
    if dj is None:
        raise HTTPException(status_code=404, detail="DJ not found")

    path = generate_dj_avatar(dj, config)
    if path is None:
        # generate_dj_avatar logs the specific failure; the response stays
        # generic so we don't leak whether it was a key, a quota, or a
        # transient API error.
        raise HTTPException(
            status_code=502,
            detail="Avatar generation failed — check server logs for details.",
        )

    return {
        "url": f"/media/dj-icon/{dj_id}",
        "generated_at": int(time.time()),
    }


# Bundled "seed" avatars that ship with the repo. Served as a fallback when
# the user hasn't (yet) regenerated for one of the seed DJs. Keyed by the
# same UUIDs as the seed roster in example-radio_config.json, so a fresh
# clone gets a fully-illustrated roster out of the box.
DJ_AVATAR_SEED_DIR = Path("app/seed/dj_icons")


@app.get("/media/dj-icon/{dj_id}")
def media_dj_icon(dj_id: str):
    """Serve a DJ's avatar PNG, or 404 if neither the generated nor the
    seed copy exists.

    Resolution order:
      1. ``generated/dj_icons/{dj_id}.png`` — user-regenerated (latest).
      2. ``app/seed/dj_icons/{dj_id}.png`` — bundled with the repo.

    Generated wins so a regenerate always shadows the seed, even if the
    DJ originated in the seed roster. New clones with no /generated yet
    fall through to the seed and see the shipped avatars.

    No path traversal risk: dj_id comes from the path segment and is
    appended to a fixed directory + ".png" suffix; FastAPI rejects
    slashes in path params by default.
    """
    generated_path = DJ_AVATAR_DIR / f"{dj_id}.png"
    if generated_path.exists():
        return FileResponse(str(generated_path), media_type="image/png")
    seed_path = DJ_AVATAR_SEED_DIR / f"{dj_id}.png"
    if seed_path.exists():
        return FileResponse(str(seed_path), media_type="image/png")
    raise HTTPException(status_code=404, detail="No avatar for this DJ")


def _warm_caches_background(config: AppConfig) -> None:
    """Pre-generate the things the first transition would otherwise have to wait for.

    Spawned from player_play in a daemon thread. By the time the user gets to
    their first DJ break / news segment / ad / skip, these are ready:
      1. Station-ID phrases (idempotent — only does work on a fresh station name)
      2. News bulletin (caches for 20–30 min via get_news_clip's own TTL)
      3. At least one station-ID stinger clip in the DB pool (needed by
         /player/stinger-url for the skip-stinger to have something to play)
    """
    try:
        if config.alerts.station_id.enabled:
            phrases = get_station_id_phrases(config)
        else:
            phrases = []

        if config.alerts.news.enabled:
            get_news_clip(config)

        # Ensure the skip-stinger pool has at least one clip. Without this the
        # user's first hit on Next has no stinger to cover the dead air.
        if config.alerts.station_id.enabled and phrases:
            db = SessionLocal()
            try:
                has_stinger = (
                    db.query(DJClip)
                    .filter(DJClip.audio_path.like("%/station_ids/%"))
                    .first()
                )
                if has_stinger is None:
                    station = active_station(config.station, config)
                    voice = station.voice or None
                    try:
                        provider = build_tts_provider(config)
                    except ValueError:
                        provider = build_tts_provider(
                            config.model_copy(update={"tts_provider": "tone"})
                        )
                    sid_text = random.choice(phrases)
                    try:
                        get_or_create_dj_clip(
                            db,
                            script_text=sid_text,
                            voice=voice,
                            voice_instructions=station.voice_instructions,
                            provider=provider,
                            clip_type="station_ids",
                        )
                        _log_event("warmup.stinger_seeded", phrase=sid_text[:60])
                    except RuntimeError:
                        logger.warning("Stinger warmup TTS failed; pool will fill on first ad break instead")
            finally:
                db.close()
    except Exception:  # noqa: BLE001
        logger.exception("Cache warmup failed")


@app.post("/player/play", response_model=PlayerActionResponse)
def player_play(payload: PlayerPlayRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _log_event("player.play.requested", queue_size=payload.queue_size, seed=payload.seed)
    config = load_config()

    try:
        queue = build_station_queue(db=db, config=config, size=payload.queue_size, seed=payload.seed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    sequence = [
        {"type": "track", "track_id": track.id, "label": _track_label(track)}
        for track in queue["tracks"]
    ]

    state.is_playing = True
    state.queue_json = json.dumps(sequence)
    state.queue_index = 0
    state.current_track_id = sequence[0].get("track_id") if sequence else None
    state.last_error = None
    db.commit()
    db.refresh(state)
    _log_event("player.play.started", queue_items=len(sequence))

    # Fire-and-forget cache warmup so the first transition (DJ/news/ad/stinger)
    # doesn't pay LLM+TTS latency. Won't block playback if it fails.
    threading.Thread(target=_warm_caches_background, args=(config,), daemon=True).start()

    return PlayerActionResponse(state=_build_player_state_response(db, state), action="play")


# ── Segment attachment helpers ────────────────────────────────────────────────
# Each returns the data needed by PlayerNextResponse for one optional segment.
# They keep player_next focused on orchestration: cadence checks and assembly.


def _attach_news(config: AppConfig) -> tuple[str | None, str | None]:
    """Return (clip_url, script_text) for the news segment, or (None, None).

    On cache miss, get_news_clip has already spawned a refresh. We give that
    refresh a bounded window to finish (NEWS_BLOCK_ON_MISS_S) before falling
    back to skipping the segment. This avoids the failure mode where sparse
    news cadence + an expired cache = no news for a long stretch.
    """
    entry = get_news_clip(config)
    if not entry:
        entry = wait_for_fresh_news(NEWS_BLOCK_ON_MISS_S)
        if not entry:
            return None, None
        _log_event("news.cache.block_on_miss.satisfied", age_s=round(time.time() - entry["generated_at"]))
    age_s = round(time.time() - entry["generated_at"])
    _log_event("player.next.news_attached", level=logging.DEBUG, source=config.alerts.news.rss_url, age_s=age_s)
    return f"/media/dj-clip/{entry['clip_hash']}", entry["script_text"]


def _attach_ad(
    db: Session, station: StationConfig, config: AppConfig, provider,
) -> tuple[str | None, str | None]:
    """Return (clip_url, script_text). script_text may be set even if clip is None."""
    ads_cfg = config.alerts.ads
    ad_clip: DJClip | None = None
    ad_script_text: str | None = None
    ad_cached = False

    pool_count = db.query(DJClip).filter(DJClip.is_ad == True).count()  # noqa: E712
    if pool_count >= ads_cfg.pool_size:
        ad_clip = random.choice(db.query(DJClip).filter(DJClip.is_ad == True).all())  # noqa: E712
        ad_script_text = ad_clip.script_text
        ad_cached = True
        _log_event("player.next.ad_pool_hit", level=logging.DEBUG, pool_count=pool_count)
    else:
        ad_script_text = generate_ad_script(station, config)
        if ad_script_text:
            ad_voice_cfg = random.choice(ads_cfg.voices) if ads_cfg.voices else None
            ad_voice = ad_voice_cfg.voice if ad_voice_cfg else None
            ad_instructions = ad_voice_cfg.voice_instructions if ad_voice_cfg else None
            try:
                ad_clip, _, ad_cached = get_or_create_dj_clip(
                    db, script_text=ad_script_text, voice=ad_voice,
                    voice_instructions=ad_instructions, provider=provider,
                    is_ad=True, clip_type="ads",
                )
            except RuntimeError:
                logger.warning("Ad clip synthesis failed with voice=%r; retrying with default", ad_voice)
                ad_clip, _, ad_cached = get_or_create_dj_clip(
                    db, script_text=ad_script_text, voice=None, provider=provider,
                    is_ad=True, clip_type="ads",
                )

    if ad_clip is None:
        return None, ad_script_text

    _log_event("player.next.ad_attached", level=logging.DEBUG, ad_cached=ad_cached, pool_count=pool_count)
    return f"/media/dj-clip/{ad_clip.script_hash}", ad_script_text


def _attach_station_id(
    db: Session, station: StationConfig, voice: str | None, config: AppConfig, provider,
) -> str | None:
    """Return the clip_url for a station-ID stinger, or None if disabled / failed."""
    if not config.alerts.station_id.enabled:
        return None
    phrases = get_station_id_phrases(config)
    if not phrases:
        return None
    sid_text = random.choice(phrases)
    try:
        sid_clip, _, sid_cached = get_or_create_dj_clip(
            db, script_text=sid_text, voice=voice,
            voice_instructions=station.voice_instructions,
            provider=provider, clip_type="station_ids",
        )
    except RuntimeError:
        logger.warning("Station ID synthesis failed with voice=%r; skipping", voice)
        return None
    if sid_clip is None:
        return None
    _log_event("player.next.station_id_attached", level=logging.DEBUG, phrase=sid_text[:60], cached=sid_cached)
    return f"/media/dj-clip/{sid_clip.script_hash}"


@app.post("/player/next", response_model=PlayerNextResponse)
def player_next(payload: PlayerNextRequest | None = None, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    reason = payload.reason if payload else None
    _log_event("player.next.requested", level=logging.DEBUG, current_index=state.queue_index, reason=reason)

    if not state.queue_json:
        raise HTTPException(status_code=400, detail="No queue available")

    queue = json.loads(state.queue_json)
    if not queue:
        raise HTTPException(status_code=400, detail="Queue is empty")

    config = load_config()
    current_idx = state.queue_index

    previous_track: Track | None = None
    if 0 <= current_idx < len(queue):
        item = queue[current_idx]
        if item.get("type") == "track" and item.get("track_id") is not None:
            previous_track = db.query(Track).filter(Track.id == item["track_id"]).first()

    next_idx = current_idx + 1
    if next_idx >= len(queue):
        raise HTTPException(status_code=400, detail="Already at end of queue")

    next_item = queue[next_idx]
    if next_item.get("type") != "track" or next_item.get("track_id") is None:
        raise HTTPException(status_code=400, detail="Next queue item is not a track")

    next_track = db.query(Track).filter(Track.id == next_item["track_id"]).first()
    if next_track is None:
        raise HTTPException(status_code=404, detail="Next track not found in library")

    station = active_station(config.station, config)

    # Cadence: include weather/news every Nth break (queue_index proxies the break count).
    def _on_cadence(every: int) -> bool:
        return every > 0 and next_idx % every == 0

    try:
        provider = build_tts_provider(config)
    except ValueError:
        provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))
    voice = station.voice or None

    # Try the prefetch cache first (skip invalidates it — different prompt context).
    cached_prefetch = prefetch.take_prefetched(next_idx) if reason != "skip" else None
    clip = None
    script_text: str | None = None
    dj_cached = False
    if cached_prefetch:
        clip = db.query(DJClip).filter(DJClip.script_hash == cached_prefetch["clip_hash"]).first()
        if clip:
            script_text = cached_prefetch["script_text"]
            dj_cached = True
            _log_event("player.next.prefetch_hit", level=logging.DEBUG, target_idx=next_idx)

    if clip is None:
        include_weather = config.alerts.weather.enabled and _on_cadence(config.alerts.weather.every_n_breaks)
        news_break_follows = config.alerts.news.enabled and _on_cadence(config.alerts.news.every_n_breaks)
        ad_break_follows = config.alerts.ads.enabled and _on_cadence(config.alerts.ads.every_n_breaks)
        effective_reason = reason
        if next_item.get("requested") and reason != "skip":
            effective_reason = "request"

        script_response = generate_dj_script(
            station,
            DJScriptGenerateRequest(
                max_sentences=3,
                reason=effective_reason,
                include_weather=include_weather,
                news_break_follows=news_break_follows,
                ad_break_follows=ad_break_follows,
            ),
            previous_track,
            next_track,
            config=config,
        )
        script_text = script_response.script_text

        instructions = station.voice_instructions or None
        t0 = time.perf_counter()
        try:
            clip, _audio_path, dj_cached = get_or_create_dj_clip(
                db, script_text=script_text, voice=voice, provider=provider,
                voice_instructions=instructions, clip_type="transitions",
            )
        except RuntimeError:
            logger.warning("DJ clip synthesis failed with voice=%r; retrying with default voice", voice)
            clip, _audio_path, dj_cached = get_or_create_dj_clip(
                db, script_text=script_text, voice=None, provider=provider, clip_type="transitions",
            )
        elapsed = time.perf_counter() - t0
        logger.debug("DJ clip ready", extra={"elapsed_s": round(elapsed, 2), "cached": dj_cached})
    if clip is None:
        raise HTTPException(status_code=500, detail="Failed to synthesize DJ clip")

    # Optional segments. Each helper is responsible for its own logging, cache
    # interaction, and error handling; cadence checks stay here.
    news_clip_url: str | None = None
    news_script_text: str | None = None
    if config.alerts.news.enabled and _on_cadence(config.alerts.news.every_n_breaks):
        news_clip_url, news_script_text = _attach_news(config)

    ad_clip_url: str | None = None
    ad_script_text: str | None = None
    if config.alerts.ads.enabled and _on_cadence(config.alerts.ads.every_n_breaks):
        ad_clip_url, ad_script_text = _attach_ad(db, station, config, provider)

    # Station ID stinger throws back to music after any non-music segment
    # (news or ad). Without one after news, the bulletin runs straight into
    # the next track which feels jarring; the stinger acts as a soft handoff.
    station_id_clip_url: str | None = None
    if ad_clip_url or news_clip_url:
        station_id_clip_url = _attach_station_id(db, station, voice, config, provider)

    state.queue_index = next_idx
    state.current_track_id = next_track.id
    db.commit()

    look_ahead_track: Track | None = None
    look_ahead_idx = next_idx + 1
    if look_ahead_idx < len(queue):
        la_item = queue[look_ahead_idx]
        if la_item.get("type") == "track" and la_item.get("track_id") is not None:
            look_ahead_track = db.query(Track).filter(Track.id == la_item["track_id"]).first()

    _log_event(
        "player.next.completed",
        level=logging.DEBUG,
        new_index=next_idx,
        track_id=next_track.id,
        dj_cached=dj_cached,
        ad_attached=bool(ad_clip_url),
    )

    return PlayerNextResponse(
        current_track_url=f"/media/track/{next_track.id}",
        current_track_metadata=TrackOut.model_validate(next_track),
        current_track_label=_track_label(next_track),
        dj_clip_url=f"/media/dj-clip/{clip.script_hash}",
        ad_clip_url=ad_clip_url,
        ad_script=ad_script_text,
        news_clip_url=news_clip_url,
        news_script=news_script_text,
        station_id_clip_url=station_id_clip_url,
        next_track_url=f"/media/track/{look_ahead_track.id}" if look_ahead_track else None,
        next_track_metadata=TrackOut.model_validate(look_ahead_track) if look_ahead_track else None,
        dj_script=script_text or "",
    )


@app.post("/player/prefetch", status_code=202)
def player_prefetch(db: Session = Depends(get_db)):
    """Called by the client ~20 s before a track ends to pre-generate the next DJ clip."""
    state = _get_or_create_player_state(db)
    if not state.is_playing or not state.queue_json:
        return {"status": "idle"}
    queue = json.loads(state.queue_json)
    current_idx = state.queue_index
    prefetch_target = current_idx + 1
    if prefetch_target >= len(queue):
        return {"status": "end_of_queue"}
    threading.Thread(
        target=prefetch.prefetch_dj_clip,
        args=(prefetch_target, list(queue), current_idx),
        daemon=True,
    ).start()
    _log_event("player.prefetch.requested", level=logging.DEBUG, target_idx=prefetch_target)
    return {"status": "scheduled"}


@app.get("/player/stinger-url", response_model=StingerUrlResponse)
def player_stinger_url(db: Session = Depends(get_db)):
    """Return a random cached station-ID clip URL for the client to play during
    the dead-air gap after a user-initiated skip. No LLM/TTS work — just a DB pick."""
    clip = (
        db.query(DJClip)
        .filter(DJClip.audio_path.like("%/station_ids/%"))
        .order_by(func.random())
        .first()
    )
    if clip is None:
        return StingerUrlResponse()
    return StingerUrlResponse(clip_url=f"/media/dj-clip/{clip.script_hash}")


@app.post("/tts/preview", response_model=TTSPreviewResponse)
def tts_preview(payload: TTSPreviewRequest, db: Session = Depends(get_db)):
    """Synthesise an arbitrary sample line for previewing a voice + instructions.

    Reuses the same get_or_create_dj_clip cache, so identical (text, voice,
    instructions) triples produce one clip and replay instantly thereafter.
    Stored under generated/previews/ to keep them separate from the
    on-air pools.
    """
    config = load_config()
    try:
        provider = build_tts_provider(config)
    except ValueError:
        provider = build_tts_provider(config.model_copy(update={"tts_provider": "tone"}))
    try:
        clip, _, _ = get_or_create_dj_clip(
            db,
            script_text=payload.text,
            voice=payload.voice,
            voice_instructions=payload.voice_instructions,
            provider=provider,
            clip_type="previews",
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"TTS provider failed: {exc}") from exc
    if clip is None:
        raise HTTPException(status_code=500, detail="Failed to synthesize preview clip")
    return TTSPreviewResponse(clip_url=f"/media/dj-clip/{clip.script_hash}")


@app.post("/player/stop", response_model=PlayerActionResponse)
def player_stop(db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    _log_event("player.stop.requested")
    state.is_playing = False
    db.commit()
    db.refresh(state)
    _log_event("player.stop.completed")
    return PlayerActionResponse(state=_build_player_state_response(db, state), action="stop")


@app.get("/player/state", response_model=PlayerStateResponse)
def get_player_state(db: Session = Depends(get_db)):
    return _build_player_state_response(db, _get_or_create_player_state(db))


@app.put("/player/state", response_model=PlayerStateResponse)
def update_player_state(payload: PlayerStateUpdateRequest, db: Session = Depends(get_db)):
    state = _get_or_create_player_state(db)
    if payload.is_playing is not None:
        state.is_playing = payload.is_playing
    if payload.volume is not None:
        state.volume = payload.volume
    db.commit()
    db.refresh(state)
    return _build_player_state_response(db, state)


