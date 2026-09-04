#!/usr/bin/env python3
"""
test_loop.py — Automated end-to-end test for the Greengrass Bedrock IPC demo.

Validates the full message loop WITHOUT EC2, Greengrass, or real Bedrock:
  1. POST /ask with a known question
  2. Assert an answer is published to demo/answer (topic A)
  3. Assert WordCounter receives it and publishes correct word count to demo/wordcount (topic B)
  4. Assert the count matches the actual word count of the canned answer

Run:  python3 test/test_loop.py  (from the project root)
Exit: 0 on all PASS, non-zero on any FAIL.
"""

import os
import sys
import time
import threading

# ---------------------------------------------------------------------------
# Step 0: Install mocks BEFORE any component code is imported
# ---------------------------------------------------------------------------

# Add test/ dir to path so mock modules are importable
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TEST_DIR)

# Add component artifact dirs to path so server.py and counter.py are importable
PROJECT_ROOT = os.path.dirname(TEST_DIR)
WEBAPP_DIR = os.path.join(PROJECT_ROOT, "components", "com.demo.WebApp", "artifacts")
COUNTER_DIR = os.path.join(PROJECT_ROOT, "components", "com.demo.WordCounter", "artifacts")
sys.path.insert(0, WEBAPP_DIR)
sys.path.insert(0, COUNTER_DIR)

# Install the IPC mock into sys.modules (must happen before importing server/counter)
import mock_ipc
mock_ipc.install()

# Install the Bedrock mock by patching boto3.client
import mock_bedrock
import boto3
boto3.client = mock_bedrock.create_mock_boto3_client

# ---------------------------------------------------------------------------
# Step 1: Import component modules (they'll use our mocked IPC + Bedrock)
# ---------------------------------------------------------------------------
import server  # noqa: E402 — must come after mock installation
import counter  # noqa: E402

# ---------------------------------------------------------------------------
# Test state
# ---------------------------------------------------------------------------
results: list[tuple[str, bool, str]] = []  # (test_name, passed, detail)


def record(name: str, passed: bool, detail: str = ""):
    status = "PASS" if passed else "FAIL"
    msg = f"  [{status}] {name}"
    if detail:
        msg += f" — {detail}"
    print(msg)
    results.append((name, passed, detail))


# ---------------------------------------------------------------------------
# Step 2: Wire up the mock IPC broker and connect both components
# ---------------------------------------------------------------------------
print("\n=== Greengrass Bedrock IPC Demo — Local Test Harness ===\n")
print("[setup] Installing mock IPC broker and mock Bedrock client...")

# Reset broker state
mock_ipc.broker.reset()

# Connect the WebApp IPC client
server.ipc_client = mock_ipc.connect()

# Connect the WordCounter IPC client
counter.ipc_client = mock_ipc.connect()

# Subscribe the WordCounter to demo/answer (topic A)
# This uses counter.py's AnswerHandler which will publish to demo/wordcount
counter.subscribe_answer()

# Subscribe the WebApp to demo/wordcount (topic B)
server.subscribe_wordcount()

print("[setup] Both components connected and subscribed.\n")

# ---------------------------------------------------------------------------
# Step 3: Simulate POST /ask via Flask test client
# ---------------------------------------------------------------------------
TEST_QUESTION = "What is the capital of France"

print(f"[test] Simulating POST /ask with question: '{TEST_QUESTION}'")
print("")

# Use Flask's test client (no real HTTP server needed)
server.app.config["TESTING"] = True
with server.app.test_client() as client:
    response = client.post(
        "/ask",
        json={"question": TEST_QUESTION},
        content_type="application/json",
    )

response_data = response.get_json()
http_status = response.status_code

# ---------------------------------------------------------------------------
# Step 4: Assertions
# ---------------------------------------------------------------------------
print("--- Assertions ---\n")

# 4a. HTTP response is 200 with an answer
record(
    "POST /ask returns 200",
    http_status == 200,
    f"got {http_status}",
)

answer_text = response_data.get("answer", "") if response_data else ""
record(
    "Response contains non-empty answer",
    bool(answer_text),
    f"answer length={len(answer_text)}",
)

# 4b. Answer was published to demo/answer (topic A)
topic_a_messages = mock_ipc.broker.published.get("demo/answer", [])
record(
    "Answer published to demo/answer (topic A)",
    len(topic_a_messages) >= 1,
    f"messages on topic A: {len(topic_a_messages)}",
)

# Verify the published content matches what was returned to the browser
if topic_a_messages:
    published_text = topic_a_messages[0].decode("utf-8")
    record(
        "Published answer matches HTTP response",
        published_text == answer_text,
        f"published={published_text[:60]}...",
    )
else:
    record("Published answer matches HTTP response", False, "no messages on topic A")

# 4c. WordCounter received it and published to demo/wordcount (topic B)
topic_b_messages = mock_ipc.broker.published.get("demo/wordcount", [])
record(
    "Word count published to demo/wordcount (topic B)",
    len(topic_b_messages) >= 1,
    f"messages on topic B: {len(topic_b_messages)}",
)

# 4d. Word count is correct
expected_count = mock_bedrock.expected_word_count(TEST_QUESTION)
if topic_b_messages:
    actual_count_str = topic_b_messages[0].decode("utf-8")
    try:
        actual_count = int(actual_count_str)
    except ValueError:
        actual_count = -1
    record(
        "Word count value is correct",
        actual_count == expected_count,
        f"expected={expected_count}, actual={actual_count}",
    )
else:
    record("Word count value is correct", False, "no messages on topic B")

# 4e. Verify answer matches expected canned answer
expected_answer = mock_bedrock.canned_answer(TEST_QUESTION)
record(
    "Bedrock answer matches expected canned response",
    answer_text == expected_answer,
    f"expected_len={len(expected_answer)}, actual_len={len(answer_text)}",
)

# ---------------------------------------------------------------------------
# Step 5: Summary
# ---------------------------------------------------------------------------
print("\n--- Summary ---\n")
total = len(results)
passed = sum(1 for _, p, _ in results if p)
failed = total - passed

print(f"  Total: {total}  |  Passed: {passed}  |  Failed: {failed}")

if failed == 0:
    print("\n  ✓ ALL TESTS PASSED — full IPC message loop validated locally.\n")
    sys.exit(0)
else:
    print("\n  ✗ SOME TESTS FAILED — see details above.\n")
    sys.exit(1)
