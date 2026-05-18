from __future__ import annotations

import hashlib
import json
import logging
import math
import struct
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

from sqlalchemy.orm import Session

from .config import AppConfig
from .models import DJClip

logger = logging.getLogger(__name__)


class ToneTTSProvider:
    """Local placeholder TTS provider that creates a short WAV tone."""

    def synthesize(self, text: str, voice: str, output_path: Path, instructions: str | None = None) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        logger.debug("Synthesizing local tone clip", extra={"voice": voice, "output_path": str(output_path), "text_len": len(text)})
        duration_seconds = max(0.4, min(2.4, 0.4 + (len(text) / 220.0)))
        sample_rate = 22050
        amplitude = 16000
        base_freq = 440 if voice == "default" else 523

        total_samples = int(sample_rate * duration_seconds)
        with wave.open(str(output_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(sample_rate)
            frames = bytearray()
            for i in range(total_samples):
                sample = amplitude * math.sin(2 * math.pi * base_freq * (i / sample_rate))
                frames.extend(struct.pack("<h", int(sample)))
            wav_file.writeframes(bytes(frames))




class OpenAITTSProvider:
    """Cloud TTS provider backed by OpenAI audio/speech endpoint."""

    def __init__(self, api_key: str, model: str, voice: str) -> None:
        self.api_key = api_key
        self.model = model
        self.voice = voice

    def synthesize(self, text: str, voice: str, output_path: Path, instructions: str | None = None) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        requested_voice = (voice or "").strip()
        resolved_voice = self.voice if not requested_voice or requested_voice == "default" else requested_voice
        logger.debug("Synthesizing OpenAI TTS clip", extra={"voice": resolved_voice, "output_path": str(output_path), "text_len": len(text), "model": self.model})
        payload = {"model": self.model, "voice": resolved_voice, "input": text, "response_format": "mp3"}
        if instructions:
            payload["instructions"] = instructions
        request = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=40) as response:  # noqa: S310
                audio_bytes = response.read()
            elapsed = time.perf_counter() - t0
            logger.debug("OpenAI TTS synthesis completed", extra={"elapsed_s": round(elapsed, 2), "model": self.model, "voice": resolved_voice, "text_len": len(text)})
        except urllib.error.HTTPError as exc:
            elapsed = time.perf_counter() - t0
            body = exc.read().decode("utf-8", errors="replace")
            logger.warning("OpenAI TTS HTTP error", extra={"status_code": exc.code, "response_body": body[:500], "elapsed_s": round(elapsed, 2)})
            raise RuntimeError(f"OpenAI TTS failed ({exc.code}): {body}") from exc
        except urllib.error.URLError as exc:
            elapsed = time.perf_counter() - t0
            logger.warning("OpenAI TTS network error", extra={"reason": str(exc.reason), "elapsed_s": round(elapsed, 2)})
            raise RuntimeError(f"OpenAI TTS network error: {exc.reason}") from exc

        output_path.write_bytes(audio_bytes)


def build_tts_provider(config: AppConfig):
    if config.tts_provider == "openai":
        if not config.openai_api_key:
            raise ValueError("openai_api_key is required when tts_provider is 'openai'")
        logger.debug("Using OpenAI TTS provider", extra={"model": config.openai_tts_model, "voice": config.openai_tts_voice})
        return OpenAITTSProvider(config.openai_api_key, config.openai_tts_model, config.openai_tts_voice)
    logger.debug("Using tone fallback TTS provider")
    return ToneTTSProvider()

def _clip_hash(script_text: str, voice: str, instructions: str | None = None) -> str:
    key = f"{voice}\n{instructions or ''}\n{script_text.strip()}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()


_CLIP_TYPE_SUBDIRS = {
    "transitions": "transitions",
    "ads": "ads",
    "news": "news",
    "station_ids": "station_ids",
    "previews": "previews",
}


def get_or_create_dj_clip(
    db: Session,
    script_text: str,
    voice: str | None = None,
    provider=None,
    voice_instructions: str | None = None,
    is_ad: bool = False,
    clip_type: str = "transitions",
) -> tuple[DJClip | None, str, bool]:
    normalized_voice = (voice or "default").strip() or "default"
    digest = _clip_hash(script_text, normalized_voice, voice_instructions)
    subdir = _CLIP_TYPE_SUBDIRS.get(clip_type, "transitions")
    clips_dir = Path("generated_audio") / subdir
    local_provider = provider or ToneTTSProvider()

    existing = db.query(DJClip).filter(DJClip.script_hash == digest).first()
    if existing:
        logger.debug("Reusing cached DJ clip", extra={"clip_id": existing.id, "voice": normalized_voice, "clip_type": clip_type})
        return existing, existing.audio_path, True

    output_path = clips_dir / f"{digest}.mp3"
    logger.debug("Generating cached DJ clip", extra={"voice": normalized_voice, "output_path": str(output_path), "clip_type": clip_type})
    local_provider.synthesize(script_text, normalized_voice, output_path, instructions=voice_instructions)

    clip = DJClip(script_text=script_text, script_hash=digest, audio_path=str(output_path), voice=normalized_voice, is_ad=is_ad)
    db.add(clip)
    db.commit()
    db.refresh(clip)
    return clip, clip.audio_path, False
