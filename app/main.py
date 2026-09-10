from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.auth import router as auth_router
from app.api.webhook import router as webhook_router
from app.api.emails import router as emails_router
from app.mcp.server import mcp


@asynccontextmanager
async def lifespan(app: FastAPI):
    # The MCP Streamable HTTP session manager needs its internal task group
    # started before any /mcp request comes in -- that only happens by
    # running mcp.session_manager.run() as part of this app's own startup.
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Gmail Email MCP", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(emails_router)

mcp.settings.streamable_http_path = "/"
app.mount("/mcp", mcp.streamable_http_app(), name="mcp")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    return {
        "service": "Gmail Email MCP",
        "version": "1.0",
        "status": "running",
        "mcp_endpoint": "https://email-connect-to-db.onrender.com/mcp",
        "endpoints": {
            "health": "/health",
            "mcp": "/mcp",
            "emails": "/emails",
            "auth": "/auth/google",
        },
    }
