import os

# Google's OAuth libs (oauthlib) refuse to run the token exchange over plain
# http://, which is what localhost uses in dev. This env var must be set
# BEFORE google-auth-oauthlib is imported anywhere (hence: top of entrypoint).
# NEVER set this in production — only for local http://localhost testing.
if os.getenv("ENVIRONMENT", "development") == "development":
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

from contextlib import asynccontextmanager
from fastapi import FastAPI
from starlette.routing import Mount
from app.api.auth import router as auth_router
from app.api.webhook import router as webhook_router
from app.api.emails import router as emails_router
from app.mcp.server import mcp


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Gmail Email MCP", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(emails_router)

# Streamable HTTP transport: single endpoint at /mcp that handles GET/POST/DELETE
# directly — this is what Claude's remote connectors (claude.ai + mcp-remote) expect.
# (The old mcp.sse_app() only exposed /mcp/sse + /mcp/messages, which 404s on /mcp itself.)
app.mount("/mcp", mcp.streamable_http_app(), name="mcp")

# Optionally disable global redirect_slashes for the whole app
app.router.redirect_slashes = False


@app.get("/health")
def health():
    return {"status": "ok"}