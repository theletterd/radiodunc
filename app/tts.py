from __future__ import annotations

import hashlib
import json
import math
import struct
import urllib.error
import urllib.request
import wave
from pathlib import Path

from sqlalchemy.orm import Session

from .config import AppConfig
from .models import DJClip


class ToneTTSProvider:
    """Local placeholder TTS provider that creates a short WAV tone."""

    def synthesize(self, text: str, voice: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
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

    def synthesize(self, text: str, voice: str, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"model": self.model, "voice": (voice or self.voice), "input": text, "format": "wav"}
        request = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:  # noqa: S310
                audio_bytes = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI TTS failed ({exc.code}): {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI TTS network error: {exc.reason}") from exc

        output_path.write_bytes(audio_bytes)


def build_tts_provider(config: AppConfig):
    if config.tts_provider == "openai":
        if not config.openai_api_key:
            raise ValueError("openai_api_key is required when tts_provider is 'openai'")
        return OpenAITTSProvider(config.openai_api_key, config.openai_tts_model, config.openai_tts_voice)
    return ToneTTSProvider()

def _clip_hash(script_text: str, voice: str) -> str:
    key = f"{voice}\n{script_text.strip()}".encode("utf-8")
    return hashlib.sha256(key).hexdigest()


def get_or_create_dj_clip(db: Session, script_text: str, voice: str | None = None, provider=None) -> tuple[DJClip, bool]:
    normalized_voice = (voice or "default").strip() or "default"
    digest = _clip_hash(script_text, normalized_voice)
    existing = db.query(DJClip).filter(DJClip.script_hash == digest).first()
    if existing:
        return existing, True

    local_provider = provider or ToneTTSProvider()
    output_path = Path("generated_audio") / f"{digest}.wav"
    local_provider.synthesize(script_text, normalized_voice, output_path)

    clip = DJClip(script_text=script_text, script_hash=digest, audio_path=str(output_path), voice=normalized_voice)
    db.add(clip)
    db.commit()
    db.refresh(clip)
    return clip, False
