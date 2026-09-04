"""
polly_tts.py — Reusable AWS Polly text-to-speech core.

SPDX-License-Identifier: MIT
Copyright (c) 2026. Provided as a sample integration under the MIT License
(see the LICENSE file). Provided "as is", without warranty of any kind.

Framework-agnostic: no Flask/Django imports here. Import this module from your
web app and call synthesize_speech() to get MP3 bytes back.

Credentials & region
---------------------
This module uses the standard boto3 credential chain — it does NOT hardcode any
access key, secret, or profile. boto3 resolves credentials in this order:
    1. Environment variables (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN)
    2. Shared config/credentials files (~/.aws/credentials, AWS_PROFILE)
    3. IAM role for Amazon EC2 / ECS / EKS / Lambda (recommended for production)

Region is read from the AWS_REGION environment variable, falling back to
POLLY_REGION, then to "us-east-1". Override by passing region= to get_polly_client().

The calling identity needs the `polly:SynthesizeSpeech` permission.
"""

from __future__ import annotations

import os
import re
import threading
from functools import lru_cache

import boto3
from botocore.exceptions import BotoCoreError, ClientError


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_VOICE = "Joanna"
DEFAULT_ENGINE = "neural"

# Polly hard limits for the Text field.
MAX_TEXT_CHARS = 3000        # plain text
MAX_TEXT_CHARS_SSML = 6000   # when TextType="ssml"


def _resolve_region(region: str | None = None) -> str:
    """Resolve the AWS region without assuming any specific caller profile."""
    return (
        region
        or os.getenv("AWS_REGION")
        or os.getenv("POLLY_REGION")
        or "us-east-1"
    )


# boto3 clients are thread-safe for calls but we guard creation.
_client_lock = threading.Lock()


@lru_cache(maxsize=4)
def _cached_client(region: str):
    return boto3.client("polly", region_name=region)


def get_polly_client(region: str | None = None):
    """Return a cached boto3 Polly client for the resolved region.

    No credentials are passed explicitly — boto3's default credential chain is
    used, so this works with env vars, shared profiles, or (best for prod) an
    attached IAM role.
    """
    resolved = _resolve_region(region)
    with _client_lock:
        return _cached_client(resolved)


# --------------------------------------------------------------------------- #
# Voice catalog
# --------------------------------------------------------------------------- #

# engine = "neural" where a neural voice is available, else "standard".
# NOTE: voice/engine availability varies by region. Verify against:
#   aws polly describe-voices --region <your-region>
VOICES = [
    {"lang": "English (US)",         "id": "Joanna",    "gender": "Female", "engine": "neural"},
    {"lang": "English (US)",         "id": "Matthew",   "gender": "Male",   "engine": "neural"},
    {"lang": "English (US)",         "id": "Ivy",       "gender": "Female", "engine": "neural"},
    {"lang": "English (British)",    "id": "Amy",       "gender": "Female", "engine": "neural"},
    {"lang": "English (British)",    "id": "Brian",     "gender": "Male",   "engine": "neural"},
    {"lang": "English (Australian)", "id": "Olivia",    "gender": "Female", "engine": "neural"},
    {"lang": "Spanish (Spain)",      "id": "Lucia",     "gender": "Female", "engine": "neural"},
    {"lang": "Spanish (US)",         "id": "Lupe",      "gender": "Female", "engine": "neural"},
    {"lang": "Spanish (Mexican)",    "id": "Mia",       "gender": "Female", "engine": "standard"},
    {"lang": "French",               "id": "Lea",       "gender": "Female", "engine": "neural"},
    {"lang": "French (Canadian)",    "id": "Gabrielle", "gender": "Female", "engine": "neural"},
    {"lang": "German",               "id": "Vicki",     "gender": "Female", "engine": "neural"},
    {"lang": "Italian",              "id": "Bianca",    "gender": "Female", "engine": "neural"},
    {"lang": "Portuguese (Brazil)",  "id": "Camila",    "gender": "Female", "engine": "neural"},
    {"lang": "Portuguese (Europe)",  "id": "Ines",      "gender": "Female", "engine": "standard"},
    {"lang": "Japanese",             "id": "Takumi",    "gender": "Male",   "engine": "neural"},
    {"lang": "Korean",               "id": "Seoyeon",   "gender": "Female", "engine": "neural"},
    {"lang": "Mandarin Chinese",     "id": "Zhiyu",     "gender": "Female", "engine": "neural"},
    {"lang": "Hindi",                "id": "Kajal",     "gender": "Female", "engine": "neural"},
    {"lang": "Arabic",               "id": "Hala",      "gender": "Female", "engine": "neural"},
    {"lang": "Dutch",                "id": "Laura",     "gender": "Female", "engine": "neural"},
    {"lang": "Polish",               "id": "Ola",       "gender": "Female", "engine": "neural"},
    {"lang": "Russian",              "id": "Tatyana",   "gender": "Female", "engine": "standard"},
    {"lang": "Turkish",              "id": "Filiz",     "gender": "Female", "engine": "standard"},
    {"lang": "Swedish",              "id": "Elin",      "gender": "Female", "engine": "neural"},
    {"lang": "Norwegian",            "id": "Ida",       "gender": "Female", "engine": "neural"},
    {"lang": "Danish",               "id": "Sofie",     "gender": "Female", "engine": "neural"},
]

VOICE_ENGINE = {v["id"]: v["engine"] for v in VOICES}

LANG_DEFAULT_VOICE = {
    "en": "Joanna", "es": "Lucia", "fr": "Lea", "de": "Vicki", "it": "Bianca",
    "pt": "Camila", "ja": "Takumi", "ko": "Seoyeon", "zh": "Zhiyu", "hi": "Kajal",
    "ar": "Hala", "nl": "Laura", "pl": "Ola", "ru": "Tatyana", "tr": "Filiz",
    "sv": "Elin", "no": "Ida", "da": "Sofie",
}

LANG_LABEL = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "ja": "Japanese", "ko": "Korean",
    "zh": "Mandarin Chinese", "hi": "Hindi", "ar": "Arabic", "nl": "Dutch",
    "pl": "Polish", "ru": "Russian", "tr": "Turkish", "sv": "Swedish",
    "no": "Norwegian", "da": "Danish",
}


# --------------------------------------------------------------------------- #
# Language detection (dependency-free, optional langdetect upgrade)
# --------------------------------------------------------------------------- #

STOPWORDS = {
    "es": {"el", "la", "los", "las", "de", "que", "y", "en", "un", "una", "es",
           "por", "con", "para", "como", "más", "pero", "su", "hola", "gracias"},
    "fr": {"le", "la", "les", "de", "un", "une", "et", "est", "que", "je", "vous",
           "pour", "pas", "ce", "dans", "bonjour", "merci", "avec", "sur"},
    "de": {"der", "die", "das", "und", "ist", "ich", "nicht", "ein", "eine", "zu",
           "den", "mit", "sie", "auf", "für", "hallo", "danke", "auch", "war"},
    "it": {"il", "lo", "la", "che", "di", "e", "un", "una", "per", "sono", "non",
           "con", "come", "ciao", "grazie", "questo", "più", "anche"},
    "pt": {"o", "a", "os", "as", "de", "que", "e", "um", "uma", "não", "com",
           "para", "por", "como", "mais", "olá", "obrigado", "você", "isso"},
    "nl": {"de", "het", "een", "en", "van", "ik", "je", "niet", "dat", "is",
           "op", "met", "voor", "hallo", "bedankt", "zijn", "maar", "ook"},
    "pl": {"i", "w", "nie", "to", "jest", "się", "na", "z", "że", "do", "co",
           "jak", "dziękuję", "cześć", "ale", "tak", "dla"},
    "sv": {"och", "att", "det", "som", "en", "på", "är", "för", "med", "jag",
           "inte", "hej", "tack", "den", "till", "har"},
    "no": {"og", "å", "det", "som", "en", "på", "er", "for", "med", "jeg",
           "ikke", "hei", "takk", "den", "til", "har"},
    "da": {"og", "at", "det", "som", "en", "på", "er", "for", "med", "jeg",
           "ikke", "hej", "tak", "den", "til", "har"},
    "en": {"the", "and", "is", "to", "of", "a", "in", "that", "it", "you",
           "for", "with", "as", "hello", "thanks", "this", "but", "not"},
}


def detect_language(text: str) -> str:
    """Best-effort language detection returning an ISO 639-1 code.

    Uses `langdetect` when installed (most accurate), otherwise falls back to a
    dependency-free heuristic (Unicode scripts + stopword overlap). Defaults to
    English when nothing matches confidently.
    """
    if not text:
        return "en"

    try:
        from langdetect import detect  # type: ignore
        code = detect(text)
        if code.startswith("zh"):
            return "zh"
        if code in LANG_DEFAULT_VOICE:
            return code
    except Exception:
        pass  # fall through to heuristic

    if re.search(r"[\u3040-\u30ff]", text):   # Hiragana / Katakana
        return "ja"
    if re.search(r"[\uac00-\ud7a3]", text):   # Hangul
        return "ko"
    if re.search(r"[\u4e00-\u9fff]", text):   # CJK ideographs
        return "zh"
    if re.search(r"[\u0600-\u06ff]", text):   # Arabic
        return "ar"
    if re.search(r"[\u0900-\u097f]", text):   # Devanagari
        return "hi"
    if re.search(r"[\u0400-\u04ff]", text):   # Cyrillic
        return "ru"

    words = re.findall(r"[a-zàâäáãåçéèêëíìîïñóòôöõúùûüýÿœæ]+", text.lower())
    if not words:
        return "en"
    word_set = set(words)
    best_lang, best_score = "en", 0
    for lang, stops in STOPWORDS.items():
        score = len(word_set & stops)
        if score > best_score:
            best_lang, best_score = lang, score
    return best_lang


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

class TTSError(Exception):
    """Raised for bad input or Polly failures. `.status` is an HTTP-style code."""
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def synthesize_speech(
    text: str,
    voice: str = DEFAULT_VOICE,
    engine: str | None = None,
    region: str | None = None,
    output_format: str = "mp3",
):
    """Synthesize `text` to speech and return (audio_bytes, metadata).

    Parameters
    ----------
    text : str
        Text to synthesize. Must be non-empty and within Polly's length limit.
    voice : str
        A Polly VoiceId, or the literal "auto" to auto-detect the language and
        choose a default voice for it.
    engine : str | None
        "neural" or "standard". If None, resolved from the voice's known engine.
    region : str | None
        AWS region override. If None, resolved from env (AWS_REGION/POLLY_REGION).
    output_format : str
        Polly OutputFormat (default "mp3").

    Returns
    -------
    (bytes, dict)
        MP3 (or requested format) audio bytes, plus a metadata dict:
        {"voice_used", "engine", "detected_language", "detected_language_code"}.

    Raises
    ------
    TTSError
        On empty/too-long text (status 400) or Polly errors (status 502).
    """
    if not text or not text.strip():
        raise TTSError("text is required and must not be empty", status=400)

    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise TTSError(
            f"text exceeds Polly's {MAX_TEXT_CHARS}-character limit "
            f"({len(text)} chars). Split into smaller chunks.",
            status=400,
        )

    detected_code = None
    detected_label = None
    if voice == "auto":
        detected_code = detect_language(text)
        detected_label = LANG_LABEL.get(detected_code, "English")
        voice = LANG_DEFAULT_VOICE.get(detected_code, DEFAULT_VOICE)

    resolved_engine = engine or VOICE_ENGINE.get(voice, DEFAULT_ENGINE)

    client = get_polly_client(region)
    try:
        resp = client.synthesize_speech(
            Text=text,
            OutputFormat=output_format,
            VoiceId=voice,
            Engine=resolved_engine,
        )
    except (BotoCoreError, ClientError) as e:
        # 502: the upstream (Polly) failed, not the client's request.
        raise TTSError(f"Polly synthesis failed: {e}", status=502) from e

    stream = resp.get("AudioStream")
    if stream is None:
        raise TTSError("Polly returned no audio stream", status=502)

    audio = stream.read()
    metadata = {
        "voice_used": voice,
        "engine": resolved_engine,
        "detected_language": detected_label,
        "detected_language_code": detected_code,
    }
    return audio, metadata


def synthesize_speech_stream(
    text: str,
    voice: str = DEFAULT_VOICE,
    engine: str | None = None,
    region: str | None = None,
    output_format: str = "mp3",
):
    """Synthesize `text` and return the raw Polly AudioStream for streaming playback.

    Identical to synthesize_speech() but returns the AudioStream object instead
    of reading all bytes into memory. Use this when you want to pipe the audio
    directly to the HTTP client (e.g. Flask streaming Response) so the browser
    can start playing before Polly finishes synthesizing the full audio.

    Returns
    -------
    (StreamingBody, dict)
        The raw Polly AudioStream (a botocore StreamingBody), plus the same
        metadata dict as synthesize_speech(). Callers must consume the stream
        (e.g. iter_chunks()) and must NOT call read() on it after returning.

    Raises
    ------
    TTSError
        On empty/too-long text (status 400) or Polly errors (status 502).
    """
    if not text or not text.strip():
        raise TTSError("text is required and must not be empty", status=400)
    text = text.strip()
    if len(text) > MAX_TEXT_CHARS:
        raise TTSError(
            f"text exceeds Polly's {MAX_TEXT_CHARS}-character limit "
            f"({len(text)} chars). Split into smaller chunks.",
            status=400,
        )

    detected_code = None
    detected_label = None
    if voice == "auto":
        detected_code = detect_language(text)
        detected_label = LANG_LABEL.get(detected_code, "English")
        voice = LANG_DEFAULT_VOICE.get(detected_code, DEFAULT_VOICE)

    resolved_engine = engine or VOICE_ENGINE.get(voice, DEFAULT_ENGINE)

    client = get_polly_client(region)
    try:
        resp = client.synthesize_speech(
            Text=text,
            OutputFormat=output_format,
            VoiceId=voice,
            Engine=resolved_engine,
        )
    except (BotoCoreError, ClientError) as e:
        raise TTSError(f"Polly synthesis failed: {e}", status=502) from e

    stream = resp.get("AudioStream")
    if stream is None:
        raise TTSError("Polly returned no audio stream", status=502)

    metadata = {
        "voice_used": voice,
        "engine": resolved_engine,
        "detected_language": detected_label,
        "detected_language_code": detected_code,
    }
    return stream, metadata
