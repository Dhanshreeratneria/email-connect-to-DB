from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.api.auth import router as auth_router
from app.api.webhook import router as webhook_router
from app.api.emails import router as emails_router
from app.mcp.server import mcp
@asynccontextmanager
async def lifespan(app:FastAPI): yield
app=FastAPI(title="Gmail Email MCP",lifespan=lifespan)
app.include_router(auth_router);app.include_router(webhook_router);app.include_router(emails_router)
# Remote MCP endpoint. Configure an API gateway/reverse proxy auth layer before exposing publicly.
app.mount("/mcp",mcp.streamable_http_app())
@app.get("/health")
def health():return {"status":"ok"}
