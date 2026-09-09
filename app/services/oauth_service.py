import json
from app.config import settings

def flow():
    if settings.google_client_secrets_json:
        config = json.loads(settings.google_client_secrets_json)
        return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=settings.google_oauth_redirect_uri)
    return Flow.from_client_secrets_file(settings.google_client_secrets_file, scopes=SCOPES, redirect_uri=settings.google_oauth_redirect_uri)