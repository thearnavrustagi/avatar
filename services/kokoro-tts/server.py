"""
Kokoro TTS + Whisper Transcription Server — OpenAI-compatible endpoints.

Usage:
    uv sync
    uv run python server.py

Serves on http://localhost:8880 by default.

Endpoints:
    POST /v1/audio/speech          — Text-to-speech (Kokoro)
    POST /v1/audio/transcriptions  — Speech-to-text (faster-whisper)
    GET  /v1/audio/voices          — List available TTS voices
    GET  /v1/models                — List available models
    GET  /health                   — Health check
"""

from __future__ import annotations

import asyncio
import io
import logging
import tempfile
from contextlib import asynccontextmanager
from typing import Optional

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

logger = logging.getLogger("kokoro-tts")

# ---------------------------------------------------------------------------
# Global model holders
# ---------------------------------------------------------------------------
pipelines: dict[str, object] = {}
whisper_model = None

SAMPLE_RATE = 24000

# Language code mapping: voice ID first character -> Kokoro lang_code
LANG_CODE_MAP: dict[str, str] = {
    "a": "a",  # American English
    "b": "b",  # British English
    "j": "j",  # Japanese
    "z": "z",  # Chinese
    "e": "e",  # Spanish
    "f": "f",  # French
    "h": "h",  # Hindi
    "i": "i",  # Italian
    "p": "p",  # Portuguese
}

# Kokoro voice IDs
VOICE_IDS = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro",
    "jm_kumo",
    "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao",
    "zm_yunjian", "zm_yunxi", "zm_yunyang",
    "ef_dora",
    "em_alex", "em_santa",
    "ff_siwis",
    "hf_alpha", "hf_beta",
    "hm_omega", "hm_psi",
    "if_sara",
    "im_nicola",
    "pf_dora",
    "pm_alex", "pm_santa",
]


def _get_pipeline(lang_code: str):
    """Get or lazily create a Kokoro pipeline for the given language code."""
    if lang_code not in pipelines:
        from kokoro import KPipeline
        pipelines[lang_code] = KPipeline(lang_code=lang_code)
        logger.info("Loaded Kokoro pipeline for lang_code=%s", lang_code)
    return pipelines[lang_code]


def _lang_code_from_voice(voice: str) -> str:
    """Extract the Kokoro language code from a voice ID's first character."""
    if voice and voice[0] in LANG_CODE_MAP:
        return LANG_CODE_MAP[voice[0]]
    return "a"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load models at startup."""
    global whisper_model

    # Pre-load the default American English pipeline
    try:
        _get_pipeline("a")
        logger.info("Default Kokoro pipeline (lang_code=a) loaded")
    except Exception:
        logger.exception("Failed to load default Kokoro pipeline")
        raise

    # Load Whisper model
    try:
        from faster_whisper import WhisperModel
        whisper_model = WhisperModel("large-v3-turbo", device="auto", compute_type="auto")
        logger.info("Whisper model (large-v3-turbo) loaded")
    except Exception:
        logger.exception("Failed to load Whisper model — transcription will be unavailable")
        # Non-fatal: TTS still works without Whisper

    yield


app = FastAPI(title="Kokoro TTS Server", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------
class SpeechRequest(BaseModel):
    model: str = "kokoro"
    input: str
    voice: str = "af_heart"
    response_format: str = "wav"
    speed: float = 1.0


class VoiceInfo(BaseModel):
    id: str
    name: str


# ---------------------------------------------------------------------------
# TTS Endpoints
# ---------------------------------------------------------------------------
def _generate_speech_sync(text: str, voice: str, speed: float) -> bytes:
    """Synchronous TTS generation — runs in a thread pool."""
    lang_code = _lang_code_from_voice(voice)
    pipe = _get_pipeline(lang_code)

    samples_list: list[np.ndarray] = []
    for result in pipe(text, voice=voice, speed=speed):
        if result.audio is not None:
            samples_list.append(result.audio)

    if not samples_list:
        raise ValueError("No audio generated")

    audio = np.concatenate(samples_list)
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@app.post("/v1/audio/speech")
async def create_speech(req: SpeechRequest) -> Response:
    if not pipelines:
        raise HTTPException(status_code=503, detail="Pipeline not loaded")

    if not req.input.strip():
        raise HTTPException(status_code=400, detail="Input text is empty")

    voice = req.voice or "af_heart"

    try:
        wav_bytes = await asyncio.to_thread(
            _generate_speech_sync, req.input, voice, req.speed,
        )
        return Response(
            content=wav_bytes,
            media_type="audio/wav",
            headers={"Content-Length": str(len(wav_bytes))},
        )
    except HTTPException:
        raise
    except Exception:
        logger.exception("TTS generation failed")
        raise HTTPException(status_code=500, detail="TTS generation failed")


# ---------------------------------------------------------------------------
# Transcription Endpoints
# ---------------------------------------------------------------------------
def _transcribe_sync(audio_path: str, language: Optional[str]) -> tuple:
    """Synchronous Whisper transcription — runs in a thread pool."""
    segments, info = whisper_model.transcribe(
        audio_path,
        language=language,
        beam_size=5,
        vad_filter=True,
    )
    return list(segments), info


@app.post("/v1/audio/transcriptions")
async def create_transcription(
    file: UploadFile = File(...),
    model: str = Form("large-v3-turbo"),
    language: Optional[str] = Form(None),
    response_format: str = Form("json"),
):
    if whisper_model is None:
        raise HTTPException(status_code=503, detail="Whisper model not loaded")

    # Write uploaded audio to a temp file for faster-whisper
    try:
        audio_bytes = await file.read()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        segments, info = await asyncio.to_thread(_transcribe_sync, tmp_path, language)
    except Exception:
        logger.exception("Transcription failed")
        raise HTTPException(status_code=500, detail="Transcription failed")
    finally:
        import os
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    full_text = "".join(seg.text for seg in segments).strip()

    if response_format == "text":
        return Response(content=full_text, media_type="text/plain")

    if response_format == "verbose_json":
        return JSONResponse({
            "text": full_text,
            "language": info.language,
            "duration": info.duration,
            "segments": [
                {
                    "id": seg.id,
                    "start": seg.start,
                    "end": seg.end,
                    "text": seg.text,
                    "avg_logprob": seg.avg_logprob,
                    "no_speech_prob": seg.no_speech_prob,
                }
                for seg in segments
            ],
        })

    # Default: json
    return JSONResponse({"text": full_text})


# ---------------------------------------------------------------------------
# Info Endpoints
# ---------------------------------------------------------------------------
@app.get("/v1/audio/voices")
async def list_voices() -> list[VoiceInfo]:
    return [VoiceInfo(id=v, name=v) for v in VOICE_IDS]


@app.get("/v1/models")
async def list_models():
    models = [
        {"id": "kokoro", "object": "model", "owned_by": "kokoro"},
    ]
    if whisper_model is not None:
        models.append({"id": "large-v3-turbo", "object": "model", "owned_by": "whisper"})
    return {"object": "list", "data": models}


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "tts_loaded": bool(pipelines),
        "whisper_loaded": whisper_model is not None,
    }


def main():
    logging.basicConfig(level=logging.INFO)
    uvicorn.run(app, host="0.0.0.0", port=8880)


if __name__ == "__main__":
    main()
