import base64,json
def decode_pubsub(payload:dict)->dict:
 encoded=payload.get("message",{}).get("data")
 if not encoded: raise ValueError("Pub/Sub message.data missing")
 return json.loads(base64.b64decode(encoded).decode())
