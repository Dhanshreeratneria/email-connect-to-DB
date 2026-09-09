from fastapi import FastAPI, Request
from starlette.routing import Mount
from app.mcp.server import mcp

app = FastAPI(title="Gmail Email MCP", lifespan=lifespan)

app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(emails_router)

# Wrap SSE app so it works at /mcp without redirect
sse_app = mcp.sse_app()

async def mcp_proxy(scope, receive, send):
    # Normalize path: treat /mcp and /mcp/ the same
    if scope["type"] == "http":
        path = scope["path"]
        if path == "/mcp":
            scope["path"] = "/mcp/"
        elif path.startswith("/mcp/"):
            pass  # already under /mcp/
        else:
            # Should not happen if mounted correctly
            pass
    await sse_app(scope, receive, send)

app.mount("/mcp", mcp_proxy)