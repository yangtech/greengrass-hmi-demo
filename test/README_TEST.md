# Test Harness — Local Validation (No AWS Required)

## What This Tests

The test harness validates the **full IPC message loop** entirely on a local dev machine, without standing up EC2, Greengrass, or calling real Bedrock:

```
POST /ask → Bedrock (mocked) → answer published to "demo/answer"
  → WordCounter receives it → word count published to "demo/wordcount"
  → correct integer word count verified
```

## How to Run (uv — recommended)

From the project root:

```bash
cd greengrass-bedrock-ipc-demo
uv venv                          # creates .venv/ with Python 3.9+
uv pip install flask boto3       # install test deps into venv
uv run python test/test_loop.py  # run the test
```

A `pyproject.toml` at the project root captures the test dependencies
(`flask`, `boto3`) so `uv pip install -e ".[test]"` also works.

## Alternative: plain pip

```bash
cd greengrass-bedrock-ipc-demo
python3 -m venv .venv && source .venv/bin/activate
pip install flask boto3
python test/test_loop.py
```

**Requirements:** Python 3.9+ with `flask` and `boto3` (no `awsiotsdk` needed — it's fully mocked).

## What Gets Mocked

| Real Dependency | Mock | Description |
|---|---|---|
| `awsiot.greengrasscoreipc` | `test/mock_ipc.py` | In-memory pub/sub broker; supports `connect()`, `PublishToTopic`, `SubscribeToTopic`, stream handler callbacks |
| Bedrock (`boto3.client("bedrock-runtime")`) | `test/mock_bedrock.py` | Returns a deterministic canned answer in the Amazon Nova response shape; no AWS credentials needed |

## What's NOT Tested

- Real Greengrass nucleus IPC authorization (`accessControl` in recipes)
- Real Bedrock model invocation / response quality (model access is auto-enabled since Sep 2025; no console opt-in needed)
- SSE streaming to a real browser (the test validates the publish side only)
- Component lifecycle (install, systemd, log routing)
- Network connectivity, IAM roles, Token Exchange Service
- AL2023-specific setup (sudoers, curl-minimal, cfn-signal)

This harness proves the **logic and wiring** — that data flows correctly between the WebApp and WordCounter via the pub/sub topic contract. Deploy to a real Greengrass core to validate the full infrastructure integration.

## Test Output

On success:

```
=== Greengrass Bedrock IPC Demo — Local Test Harness ===

[setup] Installing mock IPC broker and mock Bedrock client...
[setup] Both components connected and subscribed.

[test] Simulating POST /ask with question: 'What is the capital of France'

--- Assertions ---

  [PASS] POST /ask returns 200 — got 200
  [PASS] Response contains non-empty answer — answer length=78
  [PASS] Answer published to demo/answer (topic A) — messages on topic A: 1
  [PASS] Published answer matches HTTP response — published=You asked: What is...
  [PASS] Word count published to demo/wordcount (topic B) — messages on topic B: 1
  [PASS] Word count value is correct — expected=17, actual=17
  [PASS] Bedrock answer matches expected canned response — expected_len=78, actual_len=78

--- Summary ---

  Total: 7  |  Passed: 7  |  Failed: 0

  ✓ ALL TESTS PASSED — full IPC message loop validated locally.
```

Exit code `0` = all pass, non-zero = failure.
