"""
mock_ipc.py — Lightweight in-process mock of the Greengrass IPC local pub/sub
API surface used by server.py and counter.py.

Provides:
  - A fake `connect()` returning a MockIPCClient
  - MockIPCClient with `new_publish_to_topic()` and `new_subscribe_to_topic(handler)`
  - In-memory pub/sub broker: dict of topic -> list[handler]
  - Compatible model classes: BinaryMessage, PublishMessage, PublishToTopicRequest,
    SubscribeToTopicRequest, SubscriptionResponseMessage
  - Stream-handler base class with `on_stream_event(event)` pattern

The mock is designed so that when it's installed into sys.modules before the
component code is imported, all IPC calls route here instead of the real nucleus.
"""

import threading
from concurrent.futures import Future
from typing import Any


# ===========================================================================
# In-memory pub/sub broker (singleton)
# ===========================================================================
class _Broker:
    """Simple topic -> list[handler] broker. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: dict[str, list[Any]] = {}
        # Record of all published messages for test assertions
        self.published: dict[str, list[bytes]] = {}

    def subscribe(self, topic: str, handler: Any):
        with self._lock:
            self._subscribers.setdefault(topic, []).append(handler)

    def publish(self, topic: str, payload: bytes):
        with self._lock:
            self.published.setdefault(topic, []).append(payload)
            handlers = list(self._subscribers.get(topic, []))

        # Deliver to each subscriber's on_stream_event
        event = SubscriptionResponseMessage(
            binary_message=BinaryMessage(message=payload)
        )
        for handler in handlers:
            handler.on_stream_event(event)

    def reset(self):
        with self._lock:
            self._subscribers.clear()
            self.published.clear()


# Global broker instance
broker = _Broker()


# ===========================================================================
# Model classes (match the shapes used by the real awsiotsdk)
# ===========================================================================
class BinaryMessage:
    def __init__(self, message: bytes = b""):
        self.message = message


class JsonMessage:
    def __init__(self, message: Any = None):
        self.message = message


class PublishMessage:
    def __init__(self, binary_message: BinaryMessage | None = None,
                 json_message: JsonMessage | None = None):
        self.binary_message = binary_message
        self.json_message = json_message


class PublishToTopicRequest:
    def __init__(self, topic: str = "", publish_message: PublishMessage | None = None):
        self.topic = topic
        self.publish_message = publish_message


class SubscribeToTopicRequest:
    def __init__(self, topic: str = ""):
        self.topic = topic


class SubscriptionResponseMessage:
    def __init__(self, binary_message: BinaryMessage | None = None,
                 json_message: JsonMessage | None = None):
        self.binary_message = binary_message
        self.json_message = json_message


# ===========================================================================
# Stream handler base class
# ===========================================================================
class SubscribeToTopicStreamHandler:
    """Base class for subscription handlers — mirrors the real SDK pattern."""

    def on_stream_event(self, event: SubscriptionResponseMessage) -> None:
        pass

    def on_stream_error(self, error: Exception) -> bool:
        return True

    def on_stream_closed(self) -> None:
        pass


# ===========================================================================
# Operation stubs
# ===========================================================================
class _MockFuture:
    """Mimics a concurrent.futures.Future that resolves immediately."""

    def __init__(self, result_value=None):
        self._result = result_value

    def result(self, timeout=None):
        return self._result


class _PublishOperation:
    def __init__(self):
        self._request: PublishToTopicRequest | None = None

    def activate(self, request: PublishToTopicRequest):
        self._request = request
        # Perform the publish immediately via the broker
        payload = b""
        if request.publish_message and request.publish_message.binary_message:
            payload = request.publish_message.binary_message.message
        broker.publish(request.topic, payload)

    def get_response(self):
        return _MockFuture(result_value=None)


class _SubscribeOperation:
    def __init__(self, handler: SubscribeToTopicStreamHandler):
        self._handler = handler
        self._topic: str = ""

    def activate(self, request: SubscribeToTopicRequest):
        self._topic = request.topic
        broker.subscribe(request.topic, self._handler)

    def get_response(self):
        return _MockFuture(result_value=None)


# ===========================================================================
# Mock IPC Client
# ===========================================================================
class MockIPCClient:
    """Drop-in replacement for the object returned by awsiot.greengrasscoreipc.connect()."""

    def new_publish_to_topic(self):
        return _PublishOperation()

    def new_subscribe_to_topic(self, handler: SubscribeToTopicStreamHandler):
        return _SubscribeOperation(handler)


# ===========================================================================
# connect() — module-level function matching awsiot.greengrasscoreipc.connect()
# ===========================================================================
def connect():
    """Return a MockIPCClient (no real nucleus connection needed)."""
    return MockIPCClient()


# ===========================================================================
# Install into sys.modules so `import awsiot.greengrasscoreipc` resolves here
# ===========================================================================
import sys
import types


def install():
    """
    Inject mock modules into sys.modules so that:
      import awsiot.greengrasscoreipc          -> this module's connect()
      import awsiot.greengrasscoreipc.client   -> SubscribeToTopicStreamHandler
      import awsiot.greengrasscoreipc.model    -> model classes
    """
    # Create the awsiot package
    awsiot_pkg = types.ModuleType("awsiot")
    awsiot_pkg.__path__ = []

    # Create awsiot.greengrasscoreipc
    gg_ipc = types.ModuleType("awsiot.greengrasscoreipc")
    gg_ipc.__path__ = []
    gg_ipc.connect = connect

    # Create awsiot.greengrasscoreipc.client
    gg_client = types.ModuleType("awsiot.greengrasscoreipc.client")
    gg_client.SubscribeToTopicStreamHandler = SubscribeToTopicStreamHandler

    # Create awsiot.greengrasscoreipc.model
    gg_model = types.ModuleType("awsiot.greengrasscoreipc.model")
    gg_model.BinaryMessage = BinaryMessage
    gg_model.JsonMessage = JsonMessage
    gg_model.PublishMessage = PublishMessage
    gg_model.PublishToTopicRequest = PublishToTopicRequest
    gg_model.SubscribeToTopicRequest = SubscribeToTopicRequest
    gg_model.SubscriptionResponseMessage = SubscriptionResponseMessage

    sys.modules["awsiot"] = awsiot_pkg
    sys.modules["awsiot.greengrasscoreipc"] = gg_ipc
    sys.modules["awsiot.greengrasscoreipc.client"] = gg_client
    sys.modules["awsiot.greengrasscoreipc.model"] = gg_model
