import base64
import binascii
import json


def decode_pubsub(payload: dict) -> dict:
    """
    Decode a Google Cloud Pub/Sub push message.
    ...
    """
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValueError("Pub/Sub payload missing 'message' object")

    encoded = message.get("data")
    if not encoded:
        raise ValueError("Pub/Sub message.data missing")

    try:
        decoded_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("Pub/Sub message.data is not valid base64")

    try:
        decoded_str = decoded_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Pub/Sub message.data is not valid UTF-8")

    try:
        return json.loads(decoded_str)
    except json.JSONDecodeError:
        raise ValueError("Pub/Sub message.data is not valid JSON")