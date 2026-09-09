from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
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

# MCP SSE endpoint - proper streaming response
@app.get("/mcp")
async def mcp_sse():
    """MCP Server-Sent Events endpoint"""
    return StreamingResponse(
        mcp.sse_app(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )

# MCP info endpoint for debugging
@app.get("/mcp/info")
async def mcp_info():
    """MCP Server information"""
    return {
        "name": "Gmail Email MCP",
        "version": "1.0",
        "tools": [
            "search_emails",
            "get_email",
            "list_emails",
            "get_thread",
            "search_by_sender",
            "search_by_subject",
            "search_by_date"
        ]
    }


@app.get("/health")
def health():
    return {"status": "ok"}