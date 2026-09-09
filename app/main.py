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

# Mount MCP with redirect_slashes=False to avoid /mcp → /mcp/
app.mount("/mcp", mcp.sse_app(), name="mcp")

# Optionally disable global redirect_slashes for the whole app
app.router.redirect_slashes = False


@app.get("/health")
def health():
    return {"status": "ok"}