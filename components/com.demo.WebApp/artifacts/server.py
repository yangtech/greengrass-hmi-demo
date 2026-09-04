"""
com.demo.WebApp — Flask server for the Greengrass Bedrock IPC demo.

Responsibilities:
  1. Serve HTML UI at http://localhost:<http_port> (default 8080).
  2. POST /ask {question} → call Amazon Bedrock InvokeModel via boto3.
  3. Return answer to browser AND publish answer text to IPC topic "demo/answer".
  4. Subscribe to IPC topic "demo/wordcount"; push updates to browser via SSE.

Credentials for Bedrock come from the Greengrass Token Exchange Service (TES).
IPC messaging uses Greengrass local pub/sub (no MQTT broker).
"""

import json
import os
import queue
import sys
import threading
import time
import traceback
import datetime
import hashlib
import hmac
import ssl
import struct


# ---------------------------------------------------------------------------
# Event-stream binary framing (AWS vnd.amazon.eventstream format)
# ---------------------------------------------------------------------------
def _crc32(data: bytes) -> int:
    """CRC32 (IEEE) masked to unsigned."""
    return binascii.crc32(data) & 0xFFFFFFFF


def _encode_headers(headers: dict) -> bytes:
    """Encode headers dict into event-stream binary header format (type=7 string)."""
    encoded = b""
    for name, value in headers.items():
        n, v = name.encode("utf-8"), value.encode("utf-8")
        encoded += struct.pack("B", len(n)) + n + struct.pack("B", 7) + struct.pack(">H", len(v)) + v
    return encoded


def _encode_audio_event(audio_chunk: bytes) -> bytes:
    """Encode PCM bytes as an AWS event-stream AudioEvent binary message."""
    headers_bytes = _encode_headers({
        ":content-type": "application/octet-stream",
        ":event-type": "AudioEvent",
        ":message-type": "event",
    })
    total_length = 4 + 4 + 4 + len(headers_bytes) + len(audio_chunk) + 4
    prelude = struct.pack(">I", total_length) + struct.pack(">I", len(headers_bytes))
    prelude_crc = struct.pack(">I", _crc32(prelude))
    message = prelude + prelude_crc + headers_bytes + audio_chunk
    return message + struct.pack(">I", _crc32(message))


def _decode_event_stream_message(data: bytes) -> dict:
    """Decode one event-stream binary message, return {headers, payload}."""
    if len(data) < 12:
        return {"headers": {}, "payload": b""}
    total_length = struct.unpack(">I", data[0:4])[0]
    headers_length = struct.unpack(">I", data[4:8])[0]
    headers_bytes = data[12: 12 + headers_length]
    headers, pos = {}, 0
    while pos < len(headers_bytes):
        name_len = headers_bytes[pos]; pos += 1
        name = headers_bytes[pos:pos + name_len].decode("utf-8"); pos += name_len
        vtype = headers_bytes[pos]; pos += 1
        if vtype == 7:  # string
            vlen = struct.unpack(">H", headers_bytes[pos:pos + 2])[0]; pos += 2
            headers[name] = headers_bytes[pos:pos + vlen].decode("utf-8"); pos += vlen
        else:
            break  # unsupported type — skip rest
    payload_end = min(total_length - 4, len(data))
    payload = data[12 + headers_length: payload_end]
    return {"headers": headers, "payload": payload}


# ---------------------------------------------------------------------------
# ffmpeg WebM → PCM converter
# ---------------------------------------------------------------------------
def _ffmpeg_webm_to_pcm(webm_bytes: bytes) -> bytes:
    """Convert WebM/Opus audio → PCM (16kHz, 16-bit, mono) via ffmpeg subprocess."""
    import subprocess, shutil
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        for c in ["/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                ffmpeg = c
                break
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", "pipe:0",
         "-f", "s16le", "-ar", "16000", "-ac", "1", "-loglevel", "error", "pipe:1"],
        input=webm_bytes, capture_output=True, timeout=30,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg: {proc.stderr.decode('utf-8', 'replace')[:200]}")
    return proc.stdout



import boto3
from flask import Flask, Response, jsonify, request, send_from_directory
from flask import render_template, redirect, session

import awsiot.greengrasscoreipc
import awsiot.greengrasscoreipc.client as client
from awsiot.greengrasscoreipc.model import (
    BinaryMessage,
    PublishMessage,
    PublishToTopicRequest,
    SubscribeToTopicRequest,
    SubscriptionResponseMessage,
)

# ---------------------------------------------------------------------------
# Configuration — read from Greengrass component config (recipe.yaml defaults)
# ---------------------------------------------------------------------------
BEDROCK_MODEL_ID = os.environ.get(
    "BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0"
)
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))

TOPIC_ANSWER = "demo/answer"
TOPIC_WORDCOUNT = "demo/wordcount"

# ---------------------------------------------------------------------------
# Flask application
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static")
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "CHANGE-ME-IN-PRODUCTION")
app.template_folder = os.path.join(os.path.dirname(__file__), "templates")

# Register the Polly TTS blueprint (adds /tts/voices and /tts/synthesize routes)
from tts_blueprint import tts_bp
app.register_blueprint(tts_bp)

# Register the hybrid Cognito + local SQLite auth blueprint (adds /login,
# /logout, /me, /admin/users routes) and initialize its local user cache.
from auth import auth_bp, require_role, init_db
app.register_blueprint(auth_bp)
init_db()

# Thread-safe queue for SSE word-count events (multiple browser clients supported)
sse_clients: list[queue.Queue] = []
sse_clients_lock = threading.Lock()

# ---------------------------------------------------------------------------
# IPC client setup
# ---------------------------------------------------------------------------
ipc_client = None


def connect_ipc():
    """Connect to the Greengrass nucleus IPC."""
    global ipc_client
    try:
        ipc_client = awsiot.greengrasscoreipc.connect()
        print("[IPC] Connected to Greengrass nucleus.", flush=True)
    except Exception as e:
        print(f"[IPC] Failed to connect: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)


def publish_to_topic(topic: str, message: str):
    """Publish a text message to a Greengrass IPC local pub/sub topic."""
    try:
        publish_message = PublishMessage(
            binary_message=BinaryMessage(message=message.encode("utf-8"))
        )
        req = PublishToTopicRequest(topic=topic, publish_message=publish_message)
        operation = ipc_client.new_publish_to_topic()
        operation.activate(req)
        future = operation.get_response()
        future.result(timeout=5)
        print(f"[IPC] Published to {topic}: {message[:80]}...", flush=True)
    except Exception as e:
        print(f"[IPC] Publish error on {topic}: {e}", flush=True)
        traceback.print_exc()


# ---------------------------------------------------------------------------
# IPC Subscriber for topic B (demo/wordcount)
# ---------------------------------------------------------------------------
class WordCountHandler(client.SubscribeToTopicStreamHandler):
    """Handle incoming word-count messages from the WordCounter component."""

    def on_stream_event(self, event: SubscriptionResponseMessage) -> None:
        try:
            if event.binary_message:
                payload = event.binary_message.message.decode("utf-8")
            elif event.json_message:
                payload = json.dumps(event.json_message.message)
            else:
                return

            print(f"[IPC] Received on {TOPIC_WORDCOUNT}: {payload}", flush=True)

            # Push to all connected SSE clients
            with sse_clients_lock:
                for q in sse_clients:
                    q.put(payload)
        except Exception as e:
            print(f"[IPC] WordCount handler error: {e}", flush=True)
            traceback.print_exc()

    def on_stream_error(self, error: Exception) -> bool:
        print(f"[IPC] Stream error on {TOPIC_WORDCOUNT}: {error}", flush=True)
        return True  # keep stream alive

    def on_stream_closed(self) -> None:
        print(f"[IPC] Stream closed on {TOPIC_WORDCOUNT}.", flush=True)


def subscribe_wordcount():
    """Subscribe to the word-count IPC topic."""
    try:
        req = SubscribeToTopicRequest(topic=TOPIC_WORDCOUNT)
        handler = WordCountHandler()
        operation = ipc_client.new_subscribe_to_topic(handler)
        operation.activate(req)
        future = operation.get_response()
        future.result(timeout=5)
        print(f"[IPC] Subscribed to {TOPIC_WORDCOUNT}.", flush=True)
    except Exception as e:
        print(f"[IPC] Subscribe error on {TOPIC_WORDCOUNT}: {e}", flush=True)
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Bedrock client
# ---------------------------------------------------------------------------
bedrock_client = None


def get_bedrock_client():
    """Lazy-init the Bedrock runtime client (uses TES credentials)."""
    global bedrock_client
    if bedrock_client is None:
        bedrock_client = boto3.client(
            "bedrock-runtime",
            region_name=AWS_REGION,
        )
    return bedrock_client


def invoke_bedrock(question: str) -> str:
    """Call Bedrock InvokeModel and return the answer text."""
    br = get_bedrock_client()

    # Build request body based on model family
    if "anthropic" in BEDROCK_MODEL_ID.lower() or "claude" in BEDROCK_MODEL_ID.lower():
        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": question}],
            }
        )
        response = br.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        result = json.loads(response["body"].read())
        return result["content"][0]["text"]
    else:
        # Amazon Nova / Titan models use the converse-style body
        body = json.dumps(
            {
                "messages": [
                    {"role": "user", "content": [{"text": question}]}
                ],
                "inferenceConfig": {"maxTokens": 1024},
            }
        )
        response = br.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )
        result = json.loads(response["body"].read())
        # Amazon Nova returns output.message.content[0].text
        try:
            return result["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError):
            # Fallback: return raw result
            return json.dumps(result)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    """Serve the main UI page."""
    if "username" not in session:
        return redirect("/login")
    return send_from_directory("static", "index.html")


@app.route("/static/mic_test.html")
def mic_test():
    """Serve mic test page — requires authentication (advanced or admin)."""
    if "username" not in session:
        return redirect("/login")
    return send_from_directory("static", "mic_test.html")


@app.route("/login")
def login_page():
    """Serve the login page (auth.py handles the POST /login JSON API)."""
    return render_template("login.html")


@app.route("/admin")
def admin_page():
    """Serve the admin panel page (auth.py handles the /admin/users JSON API)."""
    if session.get("role") != "admin":
        return redirect("/login")
    return render_template("admin.html")



@app.route("/ai/status")
def ai_status():
    """Return current AI availability: Bedrock (cloud) and Ollama (local)."""
    cloud_up = is_cloud_available()
    ollama_up = is_ollama_available()
    return jsonify({
        "cloud_available": cloud_up,
        "ollama_available": ollama_up,
        "recommended_mode": "online" if cloud_up else ("offline" if ollama_up else "manual"),
    })


@app.route("/version")

def version():
    """Return the current component version."""
    # Extract version from artifact path (Greengrass stores artifacts as <component>/<version>/)
    v = os.environ.get("COMPONENT_VERSION", "")
    if not v:
        try:
            parts = os.path.abspath(__file__).split(os.sep)
            for p in parts:
                if len(p) > 2 and p[0].isdigit() and '.' in p:
                    v = p
                    break
        except Exception:
            pass
    return jsonify({"version": v or "1.2.63"})


@app.route("/ask", methods=["POST"])
def ask():
    """Handle a user question: call Bedrock, publish answer to IPC, return JSON."""
    data = request.get_json(force=True)
    question = data.get("question", "").strip()

    if not question:
        return jsonify({"error": "Empty question"}), 400

    try:
        answer = invoke_bedrock(question)
    except Exception as e:
        print(f"[Bedrock] Error: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"error": f"Bedrock error: {str(e)}"}), 500

    # Publish answer to topic A (demo/answer) via IPC
    publish_to_topic(TOPIC_ANSWER, answer)

    return jsonify({"answer": answer})


# ---------------------------------------------------------------------------
# Tic-Tac-Toe game move endpoint (Bedrock as referee + NL parser)
# ---------------------------------------------------------------------------

# Board layout: telephone keypad (7|8|9 top, 4|5|6 mid, 1|2|3 bottom)
# Internal array index mapping: idx 0=sq7, 1=sq8, 2=sq9, 3=sq4, 4=sq5, 5=sq6, 6=sq1, 7=sq2, 8=sq3
SQUARE_TO_INDEX = {7: 0, 8: 1, 9: 2, 4: 3, 5: 4, 6: 5, 1: 6, 2: 7, 3: 8}
INDEX_TO_SQUARE = {v: k for k, v in SQUARE_TO_INDEX.items()}

MOVE_SYSTEM_PROMPT = """You are a tic-tac-toe referee and natural language parser for a robot game. The board uses telephone keypad layout: 7|8|9 (top), 4|5|6 (middle), 1|2|3 (bottom). Squares are numbered 1-9. The robot plays X crosses; the human plays colored disks.

Analyze the user's input and board state, then return ONLY a JSON object with this exact schema:
{
  "valid": true/false,
  "square": <integer 1-9 or null>,
  "disk_id": "<disk ID string or null>",
  "reason": "<brief explanation>",
  "color_mentioned": "<color name or null>",
  "confidence": <float 0.0-1.0>
}

Rules:
- The human can only place a disk on an empty square (null in board_state).
- If the user mentions a color (e.g. "green", "blue", "red", "orange", "purple"), match it to the closest available disk by color.
- If no color is mentioned, use the first available disk.
- If the user says something ambiguous or not a valid move command, set valid=false and explain in reason.
- If it's not the human's turn, set valid=false.
- If the game is over, set valid=false.
- The square number must be 1-9. Common aliases: "center"=5, "top left"=7, "top right"=9, "bottom left"=1, "bottom right"=3, "top middle"/"top center"=8, "bottom middle"/"bottom center"=2, "middle left"/"left"=4, "middle right"/"right"=6.
- Return ONLY the JSON object, no markdown, no explanation outside the JSON.
- If the user's input is NOT in English, write the "reason" field in the SAME language as the user's input.
- Common speech-to-text mishears that ARE valid moves: "Bluetooth 5" means "blue to 5" (place blue disk on square 5), "red for" means "red to 4", "green ate" means "green to 8", "purple won" means "purple to 1". Interpret these charitably as game moves."""

# ---------------------------------------------------------------------------
# Local LLM (Ollama) — offline AI referee
# ---------------------------------------------------------------------------
# Tighter, simpler prompt optimized for small models (llama3.2:3b).
# Returns the same JSON schema as MOVE_SYSTEM_PROMPT.
MOVE_SYSTEM_PROMPT_LOCAL = """You are a tic-tac-toe referee. Board: 7|8|9 (top), 4|5|6 (middle), 1|2|3 (bottom). Robot plays X, human plays colored disks.

Return ONLY valid JSON (no markdown):
{"valid":true/false,"square":1-9 or null,"disk_id":"disk_0..4" or null,"reason":"brief explanation","color_mentioned":"color or null","confidence":0.0-1.0}

Rules: human can only place on empty squares. If input is not a move command, set valid=false.
Colors: disk_0=red, disk_1=blue, disk_2=green, disk_3=orange, disk_4=purple.
Aliases: center=5, top-left=7, top-right=9, bottom-left=1, bottom-right=3.
Return ONLY the JSON object."""

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")


def _invoke_ollama(user_prompt: str) -> str:
    """Call local Ollama for offline AI inference. Returns raw response text."""
    import urllib.request as _req
    import json as _json
    payload = _json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": MOVE_SYSTEM_PROMPT_LOCAL + "\n\nUser: " + user_prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.1, "num_predict": 200},
    }).encode("utf-8")
    req = _req.Request(
        f"{OLLAMA_BASE_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _req.urlopen(req, timeout=30) as r:
        result = _json.loads(r.read())
    return result.get("response", "")


def is_ollama_available() -> bool:
    """Check if local Ollama server is running."""
    import urllib.request as _req
    try:
        _req.urlopen(f"{OLLAMA_BASE_URL}/api/version", timeout=1)
        return True
    except Exception:
        return False


def is_cloud_available() -> bool:
    """Check if cloud AI (Bedrock) is reachable.

    Performs a lightweight, low-latency probe suited to intermittent
    connectivity. The instance's TES role is scoped to bedrock:InvokeModel
    only (no bedrock:ListFoundationModels), so this deliberately avoids any
    Bedrock control-plane call that would be denied and produce a false
    negative. Instead it confirms two things that together mean cloud AI is
    usable: (1) AWS credentials resolve, and (2) the bedrock-runtime endpoint
    is network-reachable. Returns False on any error (e.g. offline operation).
    """
    import socket
    try:
        # (1) Credentials must resolve (TES provides them on the instance).
        session = boto3.session.Session()
        if session.get_credentials() is None:
            return False
        # (2) bedrock-runtime endpoint must be reachable on the network.
        host = f"bedrock-runtime.{AWS_REGION}.amazonaws.com"
        with socket.create_connection((host, 443), timeout=2):
            return True
    except Exception:
        return False



COLOR_NAMES = {
    "#ef4444": "red",
    "#3b82f6": "blue",
    "#22c55e": "green",
    "#f97316": "orange",
    "#a855f7": "purple",
}


def _build_move_user_prompt(data: dict) -> str:
    """Build the user prompt describing the current game state."""
    board_state = data.get("board_state", [None] * 9)
    disks_available = data.get("disks_available", [])
    disk_colors = data.get("disk_colors", {})
    current_turn = data.get("current_turn", "human")
    game_over = data.get("game_over", False)
    user_input = data.get("user_input", "")

    # Build readable board representation
    board_display = []
    for idx in range(9):
        sq = INDEX_TO_SQUARE[idx]
        cell = board_state[idx] if idx < len(board_state) else None
        if cell is None:
            board_display.append(f"  sq{sq}: empty")
        elif cell == "X":
            board_display.append(f"  sq{sq}: X (robot)")
        else:
            color = disk_colors.get(cell, "unknown")
            color_name = COLOR_NAMES.get(color, color)
            board_display.append(f"  sq{sq}: {cell} ({color_name} disk)")

    # Build available disks description
    disk_color_names = data.get("disk_color_names", {})
    disks_desc = []
    for d in disks_available:
        color = disk_colors.get(d, "unknown")
        # Use explicit color name if provided, fall back to hex->name lookup
        color_name = disk_color_names.get(d) or COLOR_NAMES.get(color, color)
        disks_desc.append(f"  {d} ({color_name})")

    prompt = f"""Current game state:
- Turn: {current_turn}
- Game over: {game_over}

Board (telephone keypad layout):
  7 | 8 | 9  (top row)
  4 | 5 | 6  (middle row)
  1 | 2 | 3  (bottom row)

Board contents:
{chr(10).join(board_display)}

Available disks (not yet placed):
{chr(10).join(disks_desc) if disks_desc else "  (none)"}

User input: "{user_input}"

Parse this input and return the JSON response."""
    return prompt


@app.route("/move", methods=["POST"])
@require_role("advanced")
def move():
    """Parse a natural language move command using Bedrock as referee.

    Accepts game state + user input, calls Bedrock to interpret the move,
    and returns a structured JSON response indicating validity and placement.
    """
    data = request.get_json(force=True)
    user_input = data.get("user_input", "").strip()

    if not user_input:
        return jsonify({
            "valid": False,
            "square": None,
            "disk_id": None,
            "reason": "No input provided",
            "color_mentioned": None,
            "confidence": 1.0,
        })

    # Build the prompt
    user_prompt = _build_move_user_prompt(data)
    offline_mode = data.get("offline_mode", False)

    # Route to Ollama (offline) or Bedrock (online)
    if offline_mode:
        print(f"[Move] OFFLINE mode — using Ollama ({OLLAMA_MODEL})", flush=True)
        try:
            answer_text = _invoke_ollama(user_prompt)
            print(f"[Move] Ollama response: {answer_text[:100]}", flush=True)
        except Exception as e:
            print(f"[Move] Ollama error: {e}", flush=True)
            return jsonify({
                "valid": False, "square": None, "disk_id": None,
                "reason": f"Local AI error: {str(e)}. Is Ollama running?",
                "color_mentioned": None, "confidence": 0.0,
            }), 500
    else:
        # Call Bedrock with system + user prompt
        br = get_bedrock_client()
        try:
            body = json.dumps({
                "messages": [
                    {"role": "user", "content": [{"text": user_prompt}]}
                ],
                "system": [{"text": MOVE_SYSTEM_PROMPT}],
                "inferenceConfig": {"maxTokens": 512, "temperature": 0.1},
            })
            response = br.invoke_model(
                modelId=BEDROCK_MODEL_ID,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
            result = json.loads(response["body"].read())
            answer_text = result["output"]["message"]["content"][0]["text"]
        except Exception as e:
            print(f"[Move] Bedrock error: {e}", flush=True)
            traceback.print_exc()
            return jsonify({
                "valid": False, "square": None, "disk_id": None,
                "reason": f"Bedrock error: {str(e)}",
                "color_mentioned": None, "confidence": 0.0, "raw_response": None,
            }), 500

    # Parse the JSON response from Bedrock
    try:
        # Strip markdown code fences if present
        cleaned = answer_text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

        parsed = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"[Move] Failed to parse Bedrock response: {answer_text}", flush=True)
        return jsonify({
            "valid": False,
            "square": None,
            "disk_id": None,
            "reason": "Failed to parse AI response",
            "color_mentioned": None,
            "confidence": 0.0,
            "raw_response": answer_text,
        })

    # Validate and normalize the parsed response
    move_result = {
        "valid": bool(parsed.get("valid", False)),
        "square": parsed.get("square"),
        "disk_id": parsed.get("disk_id"),
        "reason": parsed.get("reason", ""),
        "color_mentioned": parsed.get("color_mentioned"),
        "confidence": float(parsed.get("confidence", 0.0)),
        "raw_response": answer_text,
        "language": data.get("language", "en"),
    }

    # If valid, convert square to board index for the frontend
    if move_result["valid"] and move_result["square"] is not None:
        sq = int(move_result["square"])
        if sq in SQUARE_TO_INDEX:
            move_result["board_index"] = SQUARE_TO_INDEX[sq]
        else:
            move_result["valid"] = False
            move_result["reason"] = f"Invalid square number: {sq}"

    print(f"[Move] input='{user_input}' -> valid={move_result['valid']}, "
          f"square={move_result['square']}, disk={move_result['disk_id']}", flush=True)

    # Publish valid moves to IPC so other components can react
    if move_result.get("valid"):
        try:
            publish_to_topic("demo/game-move", json.dumps(move_result))
        except Exception as e:
            print(f"[IPC] game-move publish failed: {e}", flush=True)

    return jsonify(move_result)


# ---------------------------------------------------------------------------
# Real-time WebSocket transcription endpoint (/ws/transcribe)
# ---------------------------------------------------------------------------
# Browser connects → sends raw PCM chunks (16kHz, 16-bit, mono, little-endian)
# Server proxies to Transcribe Streaming WS and sends back JSON messages:
#   {"type":"partial","transcript":"...","language_code":"en-US"}
#   {"type":"final","transcript":"...","language_code":"en-US"}
#   {"type":"error","error":"..."}
#   {"type":"done"}
#
# Browser side: MediaRecorder + ScriptProcessorNode or AudioWorklet for PCM conversion
# (MediaRecorder produces WebM/Opus — use ffmpeg or Web Audio API to get PCM)
# ---------------------------------------------------------------------------
import urllib.parse
import binascii

try:
    from flask_sock import Sock as _Sock
    _sock_app = _Sock()
    _HAS_FLASK_SOCK = True
except ImportError:
    _HAS_FLASK_SOCK = False
    _sock_app = None

try:
    import websockets as _ws_lib
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False

def _make_transcribe_ws_url(region: str, sample_rate: int = 16000, language_code: str = "en-US") -> str:
    """Build SigV4-presigned WebSocket URL for Amazon Transcribe Streaming.
    Correct WebSocket query param: 'sample-rate' (not media-sample-rate-hertz).
    """
    creds = boto3.session.Session().get_credentials().get_frozen_credentials()
    service = "transcribe"
    host = f"transcribestreaming.{region}.amazonaws.com:8443"
    t = datetime.datetime.utcnow()
    amz_date = t.strftime("%Y%m%dT%H%M%SZ")
    datestamp = t.strftime("%Y%m%d")
    algorithm = "AWS4-HMAC-SHA256"
    credential_scope = f"{datestamp}/{region}/{service}/aws4_request"
    params = {
        "X-Amz-Algorithm": algorithm,
        "X-Amz-Credential": f"{creds.access_key}/{credential_scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": "300",
        "X-Amz-SignedHeaders": "host",
        "language-code": language_code,
        "media-encoding": "pcm",
        "sample-rate": str(sample_rate),
    }
    if creds.token:
        params["X-Amz-Security-Token"] = creds.token
    qs = "&".join(
        f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
        for k, v in sorted(params.items())
    )
    canonical_headers = f"host:{host}\n"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join(["GET", "/stream-transcription-websocket", qs,
                                     canonical_headers, "host", payload_hash])
    string_to_sign = "\n".join([algorithm, amz_date, credential_scope,
                                  hashlib.sha256(canonical_request.encode()).hexdigest()])
    def _hmac(key, msg):
        k = key if isinstance(key, bytes) else key.encode()
        m = msg if isinstance(msg, bytes) else msg.encode()
        return hmac.new(k, m, hashlib.sha256).digest()
    signing_key = _hmac(_hmac(_hmac(_hmac(f"AWS4{creds.secret_key}".encode(), datestamp),
                                    region), service), "aws4_request")
    sig = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    url = f"wss://{host}/stream-transcription-websocket?{qs}&X-Amz-Signature={sig}"
    print(f"[Transcribe WS] URL built: lang={language_code} sr={sample_rate}Hz", flush=True)
    return url


if _HAS_FLASK_SOCK and _sock_app is not None:
    _sock_app.init_app(app)

    @_sock_app.route("/ws/transcribe")
    def ws_transcribe(ws):
        """Real-time WebSocket: browser sends raw PCM Int16 binary frames.
        Server wraps in event-stream format and relays to Amazon Transcribe.
        Transcription results sent back to browser as JSON.

        Based on aws-samples/amazon-transcribe-streaming-python-websockets pattern.
        """
        import json as _j
        import websocket as _wsc

        region = os.environ.get("TRANSCRIBE_REGION", os.environ.get("AWS_REGION", "us-east-1"))
        sample_rate = 16000
        language_code = "en-US"
        print(f"[WS Transcribe] client connected", flush=True)

        # Will be set after first audio or init message
        tc_ws = None
        stop_event = threading.Event()

        def _start_transcribe():
            nonlocal tc_ws
            url = _make_transcribe_ws_url(region, sample_rate, language_code)
            tc_ws = _wsc.create_connection(url, sslopt={"cert_reqs": ssl.CERT_REQUIRED})
            print(f"[WS Transcribe] connected to Transcribe (sr={sample_rate})", flush=True)

            def _recv_loop():
                try:
                    while not stop_event.is_set():
                        opcode, data = tc_ws.recv_data()
                        if not data:
                            break
                        msg = _decode_event_stream_message(data)
                        msg_type = msg["headers"].get(":message-type", "")
                        if msg_type == "event":
                            payload = _j.loads(msg["payload"].decode("utf-8"))
                            results = payload.get("Transcript", {}).get("Results", [])
                            if results:
                                print(f"[Transcribe WS] got {len(results)} result(s) from Transcribe", flush=True)
                            for r in results:
                                for alt in r.get("Alternatives", []):
                                    t = alt.get("Transcript", "").strip()
                                    if t:
                                        is_partial = r.get("IsPartial", True)
                                        print(f"[Transcribe WS] {'partial' if is_partial else 'FINAL'}: '{t}'", flush=True)
                                        result = {
                                            "type": "partial" if is_partial else "final",
                                            "transcript": t,
                                            "language_code": r.get("LanguageCode", language_code),
                                        }
                                        try:
                                            ws.send(_j.dumps(result))
                                        except Exception as send_err:
                                            print(f"[Transcribe WS] send error: {send_err}", flush=True)
                                            stop_event.set()
                                            return
                        elif msg_type == "exception":
                            err = msg["headers"].get(":exception-type", "Unknown")
                            body = msg["payload"].decode("utf-8", errors="replace")
                            print(f"[WS Transcribe] exception: {err}: {body}", flush=True)
                            try:
                                ws.send(_j.dumps({"type": "error", "error": f"{err}: {body}"}))
                            except Exception:
                                pass
                            stop_event.set()
                            return
                except Exception as ex:
                    print(f"[WS Transcribe] recv error: {ex}", flush=True)
                finally:
                    stop_event.set()

            t = threading.Thread(target=_recv_loop, daemon=True)
            t.start()
            return t

        reader_thread = None
        tc_started = False

        try:
            while not stop_event.is_set():
                msg = ws.receive(timeout=15)
                if msg is None:
                    break

                if isinstance(msg, bytes) and len(msg) > 0:
                    if not tc_started:
                        tc_started = True
                        reader_thread = _start_transcribe()
                        print(f"[WS Transcribe] first PCM chunk ({len(msg)}B) — Transcribe started", flush=True)
                    if tc_ws:
                        tc_ws.send_binary(_encode_audio_event(msg))

                elif isinstance(msg, str):
                    try:
                        cmd = _j.loads(msg)
                        if cmd.get("type") == "init":
                            sample_rate = int(cmd.get("sampleRate", 16000))
                            language_code = cmd.get("languageCode", "en-US")
                            print(f"[WS Transcribe] init: sr={sample_rate} lang={language_code}", flush=True)
                        elif cmd.get("type") == "end":
                            if tc_ws and tc_started:
                                tc_ws.send_binary(_encode_audio_event(b""))
                            break
                    except Exception as ex:
                        print(f"[WS Transcribe] parse error: {ex}", flush=True)

        except Exception as ex:
            print(f"[WS Transcribe] receive error: {ex}", flush=True)
        finally:
            # Signal end-of-stream to Transcribe
            stop_event.set()
            if tc_ws:
                try:
                    tc_ws.send_binary(_encode_audio_event(b""))
                except Exception:
                    pass
            # Send 'done' to browser NOW — before waiting for reader thread
            # (reader_thread.join can take up to 5s; browser would time out waiting)
            try:
                ws.send(_j.dumps({"type": "done"}))
                print("[WS Transcribe] sent done to browser", flush=True)
            except Exception:
                pass
            # Now close Transcribe WS and wait for reader
            if tc_ws:
                try:
                    tc_ws.close()
                except Exception:
                    pass
            if reader_thread:
                reader_thread.join(timeout=5)
            print("[WS Transcribe] session ended", flush=True)

else:
    print("[WS Transcribe] flask-sock not installed", flush=True)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Keep the old HTTP POST /transcribe for mic_test.html batch testing
# ---------------------------------------------------------------------------
@app.route("/transcribe", methods=["POST"])
@require_role("advanced")
def transcribe_audio():
    """HTTP POST: receive WebM blob → convert to PCM via ffmpeg → stream to Transcribe WS.
    Used by mic_test.html Step 2a for end-to-end pipeline testing."""
    audio_file = request.files.get("audio")
    if not audio_file:
        return jsonify({"transcript": "", "language_code": "en-US", "error": "No audio"}), 400
    webm_bytes = audio_file.read()
    region = os.environ.get("TRANSCRIBE_REGION", os.environ.get("AWS_REGION", "us-east-1"))
    try:
        pcm_bytes = _ffmpeg_webm_to_pcm(webm_bytes)
        print(f"[Transcribe POST] {len(webm_bytes)}B WebM → {len(pcm_bytes)}B PCM", flush=True)
    except Exception as e:
        return jsonify({"transcript": "", "language_code": "en-US", "error": f"ffmpeg: {e}"}), 500
    try:
        import websocket as _ws_client
        url = _make_transcribe_ws_url(region)
        tc_ws = _ws_client.create_connection(url, sslopt={"cert_reqs": ssl.CERT_REQUIRED})
        parts = []
        stop = threading.Event()

        def _recv():
            try:
                while not stop.is_set():
                    opcode, data = tc_ws.recv_data()
                    if not data:
                        break
                    msg = _decode_event_stream_message(data)
                    if msg["headers"].get(":message-type") == "event":
                        import json as _j
                        payload = _j.loads(msg["payload"].decode("utf-8"))
                        for r in payload.get("Transcript", {}).get("Results", []):
                            if not r.get("IsPartial"):
                                for alt in r.get("Alternatives", []):
                                    t = alt.get("Transcript", "").strip()
                                    if t:
                                        parts.append(t)
            except Exception as ex:
                print(f"[Transcribe POST recv] {ex}", flush=True)
            finally:
                stop.set()

        reader = threading.Thread(target=_recv, daemon=True)
        reader.start()
        buf = __import__('io').BytesIO(pcm_bytes)
        while True:
            chunk = buf.read(8192)
            if not chunk:
                break
            tc_ws.send_binary(_encode_audio_event(chunk))
        tc_ws.send_binary(_encode_audio_event(b""))
        reader.join(timeout=20)
        stop.set()
        tc_ws.close()
        result = {"transcript": " ".join(parts), "language_code": "en-US"}
        print(f"[Transcribe POST] result: {result}", flush=True)
        return jsonify(result)
    except Exception as e:
        print(f"[Transcribe POST] error: {e}", flush=True)
        traceback.print_exc()
        return jsonify({"transcript": "", "language_code": "en-US", "error": str(e)}), 500


# ---------------------------------------------------------------------------
# Language detection endpoint
# ---------------------------------------------------------------------------
@app.route("/detect-lang", methods=["POST"])
def detect_lang():
    """Detect language of text using langdetect.
    Accepts: {"text": "..."}  Returns: {"lang": "en"}
    """
    data = request.get_json(force=True)
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"lang": "en"})
    # Short strings (< 20 chars) are unreliable for langdetect — default to English
    # This prevents "blue to 5" from being misidentified as French/Dutch/etc.
    if len(text) < 20:
        print(f"[detect-lang] '{text}' too short for reliable detection, defaulting to en", flush=True)
        return jsonify({"lang": "en"})
    try:
        from langdetect import detect as _ld_detect
        lang = _ld_detect(text)
        lang = (lang or "en").split("-")[0]
        print(f"[detect-lang] '{text[:40]}' -> {lang}", flush=True)
        return jsonify({"lang": lang})
    except Exception as e:
        print(f"[detect-lang] error: {e}", flush=True)
        return jsonify({"lang": "en"})


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------


def main():
    print(f"[WebApp] Starting — model={BEDROCK_MODEL_ID}, region={AWS_REGION}, port={HTTP_PORT}", flush=True)
    connect_ipc()
    subscribe_wordcount()
    app.run(host="0.0.0.0", port=HTTP_PORT, threaded=True)


if __name__ == "__main__":
    main()
