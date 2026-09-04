"""
tts_blueprint.py — Flask blueprint that exposes the Polly TTS feature.

SPDX-License-Identifier: MIT
Copyright (c) 2026. Provided as a sample integration under the MIT License
(see the LICENSE file). Provided "as is", without warranty of any kind.

Drop this into an existing Flask app:

    from tts_blueprint import tts_bp
    app.register_blueprint(tts_bp)                 # routes at /tts/voices, /tts/synthesize
    # or mount under a custom prefix:
    app.register_blueprint(tts_bp, url_prefix="/api/speech")

If your app is NOT Flask, ignore this file — call polly_tts.synthesize_speech()
directly from your own handler (see README "Non-Flask integration").
"""

from flask import Blueprint, request, jsonify, Response

from polly_tts import synthesize_speech_stream, VOICES, TTSError

# All routes are namespaced under /tts by default so they won't collide with
# the host app's existing routes.
tts_bp = Blueprint("tts", __name__, url_prefix="/tts")


@tts_bp.get("/voices")
def voices():
    """Return the available voices grouped by language (for a dropdown)."""
    return jsonify(VOICES)


@tts_bp.post("/synthesize")
def synthesize():
    """Accept {"text","voice","engine"} JSON, return a streaming audio/mpeg response.

    Uses Polly's AudioStream directly — audio starts playing in the browser as
    soon as the first chunks arrive, without waiting for the full synthesis to
    complete. This is especially noticeable for longer texts.
    """
    data = request.get_json(silent=True) or {}
    try:
        audio_stream, meta = synthesize_speech_stream(
            text=data.get("text", ""),
            voice=data.get("voice", "Joanna"),
            engine=data.get("engine"),
        )
    except TTSError as e:
        return jsonify({"error": str(e)}), e.status

    headers = {"X-Voice-Used": meta["voice_used"]}
    if meta.get("detected_language"):
        headers["X-Detected-Language"] = meta["detected_language"]
        headers["X-Detected-Language-Code"] = meta["detected_language_code"]
        headers["Access-Control-Expose-Headers"] = (
            "X-Voice-Used, X-Detected-Language, X-Detected-Language-Code"
        )
    else:
        headers["Access-Control-Expose-Headers"] = "X-Voice-Used"

    def generate():
        """Yield MP3 chunks from Polly's AudioStream as they arrive."""
        for chunk in audio_stream.iter_chunks(chunk_size=4096):
            yield chunk

    # direct_passthrough=True keeps Flask from buffering the generator,
    # so chunked transfer-encoding is used and the browser starts playing
    # immediately.
    return Response(generate(), content_type="audio/mpeg", headers=headers,
                    direct_passthrough=True)
