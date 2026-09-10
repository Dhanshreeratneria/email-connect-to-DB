from contextlib import asynccontextmanager
from fastapi import FastAPI
from mcp.server.transport_security import TransportSecuritySettings
from app.api.auth import router as auth_router
from app.api.webhook import router as webhook_router
from app.api.emails import router as emails_router
from app.mcp.server import mcp


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Gmail Email MCP", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(emails_router)

mcp.settings.streamable_http_path = "/"

# FastMCP's DNS-rebinding protection rejects any Host header not on this
# allow-list. Render's domain isn't localhost, so it must be listed
# explicitly or every request gets "421 Invalid Host header".
mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=["email-connect-to-db.onrender.com"],
    allowed_origins=["*"],
)

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
