"""
transcribe Lambda function (boto3 batch — no native extension dependencies)
───────────────────────────────────────────────────────────────────────────
Receives PCM audio bytes (base64-encoded), uploads to S3, runs a Transcribe
batch job with language auto-detection, returns transcript + language code.

No awscrt or amazon-transcribe SDK needed — pure boto3 only.

Input (boto3 lambda.invoke direct):
  {"audio_b64": "<base64 PCM bytes>", "mime": "audio/pcm",
   "bucket": "<optional bucket override>"}

Output:
  {"transcript": "blue to 6", "language_code": "en-US"}

IAM permissions required:
  transcribe:StartTranscriptionJob, transcribe:GetTranscriptionJob,
  transcribe:DeleteTranscriptionJob
  s3:PutObject, s3:GetObject, s3:DeleteObject on the bucket
"""
import base64
import json
import os
import time
import uuid
import urllib.request

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
DEFAULT_BUCKET = os.environ.get("TRANSCRIBE_BUCKET", "")
LANGUAGE_OPTIONS = [
    "en-US", "en-GB", "fr-FR", "de-DE",
    "es-ES", "es-US", "it-IT", "pt-BR",
    "ja-JP", "ko-KR", "zh-CN",
]


def _transcribe_pcm(audio_bytes: bytes, bucket: str, mime: str = "audio/webm") -> dict:
    """Upload PCM to S3 and run a Transcribe batch job. Returns {transcript, language_code}."""
    job_id = f"game-{uuid.uuid4().hex[:12]}"
    mime = parsed.get("mime", "audio/webm")  # get mime early for key naming
    ext = "webm" if "webm" in mime else "pcm"
    s3_key = f"transcribe-temp/{job_id}.{ext}"

    s3 = boto3.client("s3", region_name=REGION)
    tc = boto3.client("transcribe", region_name=REGION)

    try:
        # Upload raw PCM — Transcribe accepts raw signed-16bit PCM
        s3.put_object(Bucket=bucket, Key=s3_key, Body=audio_bytes)
        print(f"[Transcribe] uploaded {len(audio_bytes)}B ({mime}) to s3://{bucket}/{s3_key}", flush=True)

        # Determine MediaFormat from mime type
        # webm → "webm", audio/pcm or audio/wav → "wav" (with proper WAV header)
        mime = parsed.get("mime", "audio/pcm")
        if "webm" in mime:
            media_format = "webm"
            extra_kwargs = {}
        else:
            media_format = "wav"
            extra_kwargs = {"MediaSampleRateHertz": 16000}

        tc.start_transcription_job(
            TranscriptionJobName=job_id,
            Media={"MediaFileUri": f"s3://{bucket}/{s3_key}"},
            MediaFormat=media_format,
            IdentifyLanguage=True,
            LanguageOptions=LANGUAGE_OPTIONS,
            **extra_kwargs,
        )

        # Poll until done (max 30s for a short clip)
        for _ in range(30):
            time.sleep(1)
            resp = tc.get_transcription_job(TranscriptionJobName=job_id)
            status = resp["TranscriptionJob"]["TranscriptionJobStatus"]
            if status in ("COMPLETED", "FAILED"):
                break

        if resp["TranscriptionJob"]["TranscriptionJobStatus"] != "COMPLETED":
            reason = resp["TranscriptionJob"].get("FailureReason", "job failed")
            return {"transcript": "", "language_code": "en-US", "error": reason}

        transcript_uri = resp["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        with urllib.request.urlopen(transcript_uri) as r:
            t_data = json.loads(r.read())

        transcript = t_data["results"]["transcripts"][0]["transcript"]
        lang = resp["TranscriptionJob"].get("LanguageCode", "en-US")
        print(f"[Transcribe] job={job_id} lang={lang} text='{transcript[:60]}'", flush=True)
        return {"transcript": transcript, "language_code": lang}

    finally:
        try: s3.delete_object(Bucket=bucket, Key=s3_key)
        except Exception: pass
        try: tc.delete_transcription_job(TranscriptionJobName=job_id)
        except Exception: pass


def lambda_handler(event, context):
    try:
        # Support direct boto3 invoke (event IS payload) and API GW (event["body"])
        if "audio_b64" in event:
            parsed = event
        elif "body" in event:
            body = event["body"]
            parsed = json.loads(body) if isinstance(body, str) else body
        else:
            return _error(400, "No audio_b64 field found in event")

        audio_b64 = parsed.get("audio_b64", "")
        if not audio_b64:
            return _error(400, "Missing audio_b64 field")

        pcm_bytes = base64.b64decode(audio_b64)
        print(f"[Transcribe Lambda] received {len(pcm_bytes)} PCM bytes", flush=True)

        bucket = parsed.get("bucket") or DEFAULT_BUCKET
        if not bucket:
            return _error(500, "TRANSCRIBE_BUCKET env var not set and no bucket in request")

        result = _transcribe_pcm(audio_bytes, bucket, parsed.get("mime","audio/webm"))
        print(f"[Transcribe Lambda] result: {result}", flush=True)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json", "Access-Control-Allow-Origin": "*"},
            "body": json.dumps(result),
        }

    except Exception as e:
        import traceback; traceback.print_exc()
        print(f"[Transcribe Lambda] unhandled error: {e}", flush=True)
        return _error(500, str(e))


def _error(code, msg):
    return {
        "statusCode": code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": msg, "transcript": "", "language_code": "en-US"}),
    }
