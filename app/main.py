from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.responses import StreamingResponse, JSONResponse
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

# Add MCP SSE endpoint using add_route (works with ASGI apps)
app.add_route("/mcp", mcp.sse_app(), methods=["GET"])


# Claude.ai OAuth authorization endpoint
@app.get("/authorize")
async def authorize(
    response_type: str = None,
    client_id: str = None,
    redirect_uri: str = None,
    code_challenge: str = None,
    code_challenge_method: str = None,
    state: str = None,
):
    """
    OAuth authorization endpoint for Claude.ai connector.
    
    Claude.ai calls this to authenticate the connector.
    """
    
    # Validate request
    if response_type != "code":
        return JSONResponse(
            status_code=400,
            content={"error": "unsupported_response_type"}
        )
    
    # For PKCE flow, generate auth code
    import secrets
    import base64
    
    # Generate authorization code
    auth_code = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    
    # Build redirect with auth code
    redirect_params = f"code={auth_code}&state={state}"
    
    if "?" in redirect_uri:
        redirect_url = f"{redirect_uri}&{redirect_params}"
    else:
        redirect_url = f"{redirect_uri}?{redirect_params}"
    
    # Redirect back to Claude.ai with authorization code
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=redirect_url)


# Connector status endpoint
@app.get("/connector/status")
async def connector_status():
    """Check connector status"""
    return {
        "status": "connected",
        "name": "Gmail Email MCP",
        "version": "1.0",
        "authenticated": True,
        "endpoints": {
            "mcp": "/mcp",
            "emails": "/emails",
            "health": "/health"
        }
    }


# MCP SSE endpoint - proper streaming response
@app.get("/mcp")
async def mcp_sse():
    """
    MCP Server-Sent Events endpoint for Claude.ai.
    """
    return StreamingResponse(
        mcp.sse_app(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


# MCP info endpoint for debugging and verification
@app.get("/mcp/info")
async def mcp_info():
    """MCP Server information"""
    return {
        "name": "Gmail Email MCP",
        "version": "1.0",
        "status": "ready",
        "url": "https://email-connect-to-db.onrender.com/mcp",
        "capabilities": {
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
    }


@app.get("/health")
def health():
    """Health check endpoint"""
    return {"status": "ok"}


@app.get("/")
def root():
    """Root endpoint with service info"""
    return {
        "service": "Gmail Email MCP",
        "version": "1.0",
        "status": "running",
        "mcp_endpoint": "https://email-connect-to-db.onrender.com/mcp",
        "endpoints": {
            "health": "/health",
            "authorize": "/authorize",
            "connector_status": "/connector/status",
            "mcp": "/mcp",
            "mcp_info": "/mcp/info",
            "emails": "/emails",
            "auth": "/auth/google"
        }
    }