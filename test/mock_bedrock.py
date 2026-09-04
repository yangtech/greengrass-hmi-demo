"""
mock_bedrock.py — Fake Bedrock runtime client for local testing.

Returns a deterministic canned answer in the same JSON shape that the Amazon
Nova / Titan model path in server.py expects (output.message.content[0].text),
so invoke_bedrock() parsing works without modification.

The canned answer echoes the question and appends a fixed sentence so the word
count is predictable and testable.
"""

import io
import json
from unittest.mock import MagicMock

# Fixed suffix appended to every answer — gives a known word count contribution
_FIXED_SUFFIX = "This is a deterministic mock answer for local testing purposes."


def canned_answer(question: str) -> str:
    """Generate the canned answer text for a given question."""
    return f"You asked: {question} {_FIXED_SUFFIX}"


def expected_word_count(question: str) -> int:
    """Return the expected word count of the canned answer."""
    return len(canned_answer(question).split())


class MockBedrockBody:
    """Mimics the StreamingBody returned by boto3 invoke_model response['body']."""

    def __init__(self, data: dict):
        self._bytes = json.dumps(data).encode("utf-8")

    def read(self) -> bytes:
        return self._bytes


class MockBedrockClient:
    """
    Drop-in replacement for boto3.client("bedrock-runtime").

    Implements invoke_model() returning a Nova-style response:
    {
        "output": {
            "message": {
                "content": [{"text": "<canned answer>"}]
            }
        }
    }
    """

    def invoke_model(self, modelId: str, contentType: str, accept: str, body: str):
        # Parse the request to extract the question
        request_body = json.loads(body)

        # Extract question from the Nova/Titan request format
        # {"messages": [{"role": "user", "content": [{"text": "..."}]}], ...}
        question = ""
        try:
            messages = request_body.get("messages", [])
            for msg in messages:
                if msg.get("role") == "user":
                    content = msg.get("content", [])
                    if isinstance(content, list) and content:
                        question = content[0].get("text", "")
                    elif isinstance(content, str):
                        question = content
                    break
        except (KeyError, IndexError, TypeError):
            question = "unknown"

        answer_text = canned_answer(question)

        # Return Nova-style response shape
        response_data = {
            "output": {
                "message": {
                    "content": [{"text": answer_text}]
                }
            }
        }

        return {"body": MockBedrockBody(response_data)}


def create_mock_boto3_client(service_name: str, **kwargs):
    """
    Replacement for boto3.client() that returns MockBedrockClient for
    "bedrock-runtime" and raises for anything else.
    """
    if service_name == "bedrock-runtime":
        return MockBedrockClient()
    raise ValueError(f"MockBedrock: unexpected service '{service_name}'")
