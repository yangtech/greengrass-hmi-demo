"""
com.demo.WordCounter — Headless IPC subscriber that counts words in Bedrock answers.

Responsibilities:
  1. Subscribe to IPC topic "demo/answer" (topic A).
  2. On each message: count words in the answer text.
  3. Publish the integer word count to IPC topic "demo/wordcount" (topic B).

Runs as a long-lived process under Greengrass nucleus.
"""

import sys
import time
import traceback

import awsiot.greengrasscoreipc
import awsiot.greengrasscoreipc.client as client
from awsiot.greengrasscoreipc.model import (
    BinaryMessage,
    PublishMessage,
    PublishToTopicRequest,
    SubscribeToTopicRequest,
    SubscriptionResponseMessage,
)

TOPIC_ANSWER = "demo/answer"
TOPIC_WORDCOUNT = "demo/wordcount"

# ---------------------------------------------------------------------------
# IPC client
# ---------------------------------------------------------------------------
ipc_client = None


def connect_ipc():
    """Connect to the Greengrass nucleus IPC."""
    global ipc_client
    try:
        ipc_client = awsiot.greengrasscoreipc.connect()
        print("[WordCounter] Connected to Greengrass nucleus IPC.", flush=True)
    except Exception as e:
        print(f"[WordCounter] Failed to connect IPC: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)


def publish_wordcount(count: int):
    """Publish the word count to topic B."""
    try:
        message_text = str(count)
        publish_message = PublishMessage(
            binary_message=BinaryMessage(message=message_text.encode("utf-8"))
        )
        req = PublishToTopicRequest(
            topic=TOPIC_WORDCOUNT, publish_message=publish_message
        )
        operation = ipc_client.new_publish_to_topic()
        operation.activate(req)
        future = operation.get_response()
        future.result(timeout=5)
        print(f"[WordCounter] Published word count: {count}", flush=True)
    except Exception as e:
        print(f"[WordCounter] Publish error: {e}", flush=True)
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Subscriber handler for topic A (demo/answer)
# ---------------------------------------------------------------------------
class AnswerHandler(client.SubscribeToTopicStreamHandler):
    """Handle incoming answer messages from the WebApp component."""

    def on_stream_event(self, event: SubscriptionResponseMessage) -> None:
        try:
            if event.binary_message:
                text = event.binary_message.message.decode("utf-8")
            elif event.json_message:
                # If JSON, stringify and count
                import json
                text = json.dumps(event.json_message.message)
            else:
                return

            word_count = len(text.split())
            print(
                f"[WordCounter] Received answer ({word_count} words): "
                f"{text[:60]}...",
                flush=True,
            )
            publish_wordcount(word_count)
        except Exception as e:
            print(f"[WordCounter] Handler error: {e}", flush=True)
            traceback.print_exc()

    def on_stream_error(self, error: Exception) -> bool:
        print(f"[WordCounter] Stream error on {TOPIC_ANSWER}: {error}", flush=True)
        return True  # keep stream alive

    def on_stream_closed(self) -> None:
        print(f"[WordCounter] Stream closed on {TOPIC_ANSWER}.", flush=True)


def subscribe_answer():
    """Subscribe to the answer IPC topic."""
    try:
        req = SubscribeToTopicRequest(topic=TOPIC_ANSWER)
        handler = AnswerHandler()
        operation = ipc_client.new_subscribe_to_topic(handler)
        operation.activate(req)
        future = operation.get_response()
        future.result(timeout=5)
        print(f"[WordCounter] Subscribed to {TOPIC_ANSWER}.", flush=True)
    except Exception as e:
        print(f"[WordCounter] Subscribe error: {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    print("[WordCounter] Starting...", flush=True)

    connect_ipc()
    subscribe_answer()

    print("[WordCounter] Running. Waiting for messages on demo/answer...", flush=True)

    # Keep the process alive
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[WordCounter] Shutting down.", flush=True)


if __name__ == "__main__":
    main()
