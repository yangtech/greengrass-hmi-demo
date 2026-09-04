# IoT Greengrass HMI Demo — Robot Arm Tic-Tac-Toe
<img width="1264" height="1084" alt="image" src="https://github.com/user-attachments/assets/b5ae0827-7b9c-4384-b078-d77282bbe188" />

A full-featured AWS IoT Greengrass v2 demo showcasing a voice-controlled Human-Machine Interface (HMI) for a FANUC robot arm playing Tic-Tac-Toe. Demonstrates edge AI, hybrid online/offline operation, real-time speech-to-text, and role-based access control — all running on a Greengrass-managed EC2 instance.

## Demo Highlights

| Feature | Online (Cloud) | Offline (Local) |
|---|---|---|
| **Game intelligence** | Amazon Bedrock (Nova Lite) | Ollama llama3.2:3b |
| **Voice commands** | Amazon Transcribe Streaming | ❌ Disabled |
| **Voice readback** | Amazon Polly (multilingual) | ❌ Disabled |
| **Manual game play** | ✅ Click / drag disks | ✅ Click / drag disks |
| **Authentication** | Amazon Cognito | Local SQLite fallback |
| **Robot arm animation** | ✅ SVG IK-based | ✅ SVG IK-based |

## Architecture



```
┌─────────────────────────── EC2 (t3.large, Greengrass v2) ───────────────────────────┐
│                                                                                       │
│  com.demo.WebApp (Flask :8080)                                                        │
│  ├── Auth: Cognito (online) / SQLite (offline)                                       │
│  ├── /move → Bedrock (online) or Ollama (offline)                                    │
│  ├── /ws/transcribe → Transcribe Streaming WebSocket (pure Python + SigV4)           │
│  ├── /tts/synthesize → Amazon Polly (neural, multilingual)                           │
│  ├── /login, /admin → Flask templates (Jinja2)                                       │
│  └── IPC pub/sub → demo/game-move, demo/answer, demo/wordcount                      │
│                                                                                       │
│  Ollama (systemd service)                                                             │
│  └── llama3.2:3b (2GB, CPU inference ~2-3s/move)                                    │
│                                                                                       │
│  com.demo.WordCounter (IPC subscriber)                                                │
│  └── demo/answer → count → demo/wordcount                                            │
│                                                                                       │
│  Greengrass Nucleus v2.14.3                                                           │
└───────────────────────────────────────────────────────────────────────────────────────┘
          │                        │                          │
          ▼                        ▼                          ▼
  Amazon Bedrock           Amazon Transcribe          Amazon Cognito
  (Nova Lite v1:0)         Streaming (WebSocket)      User Pool
                                                      + Local SQLite
          │
          ▼
  Amazon Polly (Neural TTS)
```

## Features

### 🎮 Tic-Tac-Toe Game
- SVG robot arm with calibrated inverse kinematics (shoulder, elbow, wrist)
- Three input methods: cell click, drag-to-place, voice/text commands
- AI referee: Bedrock (online) or Ollama (offline) interprets natural language moves
- Robot counter-moves via minimax algorithm (optimal play)
- Parallel-jaw gripper animation, cross pile depletion, held-piece tracking

### 🎤 Real-Time Voice Pipeline
- Browser `ScriptProcessorNode` → PCM 16kHz Int16
- WebSocket `/ws/transcribe` → `websocket-client` → Amazon Transcribe Streaming
- Pure Python SigV4 presigned URL (no `amazon-transcribe` SDK — avoids `awscrt` conflict with `awsiotsdk`)
- Event-stream binary framing (`_encode_audio_event` / `_decode_event_stream_message`)
- `normalizeGameInput()` — fixes STT mishears ("Bluetooth 5" → "blue 5")
- ~2 second real-time transcription latency

### 🔊 Polly TTS Readback
- Language-aware: detects language via `/detect-lang` (langdetect)
- Voice mapping: en→Joanna, fr→Lea, de→Vicki, es→Lucia, ja→Takumi, etc.
- Speaks: game moves, rejections, game over results, LLM responses

### 🔐 Hybrid Authentication
- **Online:** Amazon Cognito (USER_PASSWORD_AUTH) → credentials cached in local SQLite
- **Offline:** Local SQLite with Argon2id password hashing
- **Roles:** Admin (user management), Advanced (voice + AI), Basic (manual play only)
- Admin can create local users when offline; local users disabled when cloud is available
- Cognito User Pool: `<COGNITO_USER_POOL_ID>` / Client: `<COGNITO_CLIENT_ID>`

### 🤖 Online/Offline Mode Toggle
- Manual toggle switch in the UI: 🌐 Cloud Mode ↔ 🔴 Offline Mode
- Cloud mode: Bedrock + Transcribe + Polly (full capabilities)
- Offline mode: Ollama (local AI) + manual input only (mic/Polly disabled)
- `/ai/status` endpoint reports cloud and Ollama availability

## Project Structure

```
greengrass-hmi-demo/
├── README.md                          # This file
├── PHYSICAL_DEPLOY.md                 # Physical deployment guide
├── deploy.sh                          # Local Greengrass component deployment
├── bedrock-tes-policy.json            # IAM policy for Token Exchange Role
├── provision.sh                       # EC2 + Greengrass provisioning
├── .gitignore
├── infra/
│   ├── README.md                      # CloudFormation guide
│   └── greengrass-ec2.yaml            # IaC: EC2 + Greengrass + Ollama bootstrap
├── components/
│   ├── com.demo.WebApp/
│   │   ├── recipe.yaml                # Component recipe (v1.4.1)
│   │   └── artifacts/
│   │       ├── server.py              # Flask server + Transcribe WS + Ollama
│   │       ├── auth.py                # Cognito/SQLite hybrid auth blueprint
│   │       ├── polly_tts.py           # Polly TTS core
│   │       ├── tts_blueprint.py       # Polly Flask blueprint
│   │       ├── requirements.txt       # Python dependencies
│   │       ├── static/
│   │       │   ├── index.html         # Game UI (SVG arm, board, toggle)
│   │       │   ├── mic_test.html      # Audio streaming diagnostic page
│   │       │   └── tts-widget.js      # TTS browser widget
│   │       └── templates/
│   │           ├── login.html         # Login page
│   │           └── admin.html         # User management (admin only)
│   └── com.demo.WordCounter/
│       ├── recipe.yaml
│       └── artifacts/
│           ├── counter.py
│           └── requirements.txt
├── lambda/                            # (Historical — replaced by WebSocket streaming)
│   └── transcribe-streaming/
│       ├── lambda_function.py
│       └── requirements.txt
└── test/
    ├── README_TEST.md
    ├── mock_bedrock.py
    ├── mock_ipc.py
    └── test_loop.py
```

## Prerequisites

1. **AWS Account** — with access to Bedrock, Transcribe, Polly, Cognito, IoT Greengrass
2. **EC2 Instance** — Amazon Linux 2023, **t3.large** (8 GB RAM required for Ollama)
3. **IAM Roles:**
   - `GreengrassV2TokenExchangeRole` — Bedrock, Transcribe, Polly, S3
   - `GreengrassDemo-EC2Role` — same + Lambda invoke
4. **Security Group** — SSH (22) + HTTP (8080) from your IP
5. **Ollama** — installed as systemd service with `llama3.2:3b` model pulled
6. **ffmpeg** — installed at `/usr/local/bin/ffmpeg` (for WebM→PCM conversion)

## Quick Start

```bash
# 1. SSH tunnel to the EC2 instance
ssh -L 8080:localhost:8080 -i ~/.ssh/greengrass-demo.pem ec2-user@<EC2_ELASTIC_IP>

# 2. Deploy components
rsync -av -e "ssh -i ~/.ssh/greengrass-demo.pem" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.deploy-staging' --exclude '.git' \
  . ec2-user@<EC2_ELASTIC_IP>:~/greengrass-hmi-demo/
ssh -i ~/.ssh/greengrass-demo.pem ec2-user@<EC2_ELASTIC_IP> \
  "cd ~/greengrass-hmi-demo && sudo bash deploy.sh"

# 3. Open browser
open http://localhost:8080
# Login: yang / <YOUR_PASSWORD>
```

## Key Technical Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Transcribe transport | Pure Python WebSocket + SigV4 | `amazon-transcribe` SDK conflicts with `awsiotsdk` (awscrt version clash) |
| WebSocket library | `websocket-client` (sync) | Simpler than `websockets` (async); works with Flask dev server |
| Browser→Server WS | `flask-sock` | Handles binary PCM frames on Flask's threaded dev server |
| Offline LLM | Ollama `llama3.2:3b` | 2GB model, ~2-3s inference on CPU, reliable JSON output |
| Password hashing | Argon2id | OWASP 2024+ recommendation |
| Transcribe URL param | `sample-rate` (not `media-sample-rate-hertz`) | Official WebSocket API spec — different from HTTP/2 header name |

## Dependencies

```
flask==3.1.0
boto3==1.35.0
awsiotsdk==1.22.0
langdetect>=1.0.9
websockets>=12.0
flask-sock>=0.7.0
websocket-client>=1.6.0
argon2-cffi>=21.0
```

## Version History

| Version | Date | Changes |
|---|---|---|
| 1.4.1 | Sep 2026 | Repo cleanup, auth.py edits |
| 1.4.0 | Aug 28 | Offline AI mode (Ollama), EC2 resize to t3.large |
| 1.3.x | Aug 17 | Cognito + SQLite auth, RBAC, login/admin pages |
| 1.2.70 | Aug 14 | Working baseline: game + Transcribe streaming + Polly |
| 1.2.0-1.2.69 | Aug 12-14 | Game UI, arm calibration, voice pipeline, Polly |
| 1.0.0 | Aug 10 | Initial Greengrass Bedrock IPC demo |

## Infrastructure

- **Stack:** `greengrass-bedrock-demo` (CloudFormation)
- **Account:** <AWS_ACCOUNT_ID> (us-east-1)
- **EC2:** `<EC2_INSTANCE_ID>`, Elastic IP `<EC2_ELASTIC_IP>`
- **Instance type:** t3.large (8 GB RAM)
- **Cognito Pool:** `<COGNITO_USER_POOL_ID>`
- **S3 Bucket:** `greengrass-demo-transcribe-<AWS_ACCOUNT_ID>` (batch fallback)
- **Lambda:** `fanuc-transcribe-streaming` (batch fallback, not actively used)

## Documentation

- `PHYSICAL_DEPLOY.md` — Physical deployment guide
- `infra/README.md` — CloudFormation deployment guide
- `test/README_TEST.md` — Test harness documentation
