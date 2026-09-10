from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.transport_security import TransportSecuritySettings

from app.api.auth import router as auth_router
from app.api.webhook import router as webhook_router
from app.api.emails import router as emails_router
from app.config import settings
from app.mcp.server import mcp
from app.services import connector_auth
from app.services.oauth_service import authorization_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="Gmail Email MCP", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(emails_router)

mcp.settings.streamable_http_path = "/"
mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=["email-connect-to-db.onrender.com"],
    allowed_origins=["*"],
)


class RequireBearerToken:
    """Wraps the MCP ASGI app; rejects requests without a valid token
    issued by our /token endpoint."""

    def __init__(self, inner_app):
        self.inner_app = inner_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner_app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode()
        token = auth_header[7:] if auth_header.lower().startswith("bearer ") else None
        token_data = connector_auth.verify_access_token(token) if token else None

        if not token_data:
            base = settings.public_base_url.rstrip("/")
            response = JSONResponse(
                {"error": "unauthorized", "error_description": "Missing or invalid access token"},
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer realm="mcp", '
                        f'resource_metadata="{base}/.well-known/oauth-protected-resource"'
                    )
                },
            )
            await response(scope, receive, send)
            return

        await self.inner_app(scope, receive, send)


app.mount("/mcp", RequireBearerToken(mcp.streamable_http_app()), name="mcp")


@app.get("/.well-known/oauth-protected-resource")
async def protected_resource_metadata():
    base = settings.public_base_url.rstrip("/")
    return {"resource": f"{base}/mcp", "authorization_servers": [base]}


@app.get("/.well-known/oauth-authorization-server")
async def authorization_server_metadata():
    base = settings.public_base_url.rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


@app.post("/register")
async def register_client(request: Request):
    body = await request.json()
    redirect_uris = body.get("redirect_uris", [])
    client_id = connector_auth.register_client(redirect_uris)
    return JSONResponse({
        "client_id": client_id,
        "redirect_uris": redirect_uris,
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
    })


@app.get("/authorize")
@app.options("/authorize")
async def authorize(
    response_type: str = None,
    client_id: str = None,
    redirect_uri: str = None,
    code_challenge: str = None,
    code_challenge_method: str = None,
    state: str = None,
    resource: str = None,
):
    """Bounces the browser through real Google login. Only the mailbox
    owner's account (checked in /auth/google/callback) gets a code."""
    if response_type != "code" or not redirect_uri or not code_challenge:
        return JSONResponse({"error": "invalid_request"}, status_code=400)

    conn_state = connector_auth.start_authorization(
        client_id=client_id,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        state=state,
        resource=resource,
    )

    google_auth_url, google_state = authorization_url()
    connector_auth.map_google_state(google_state, conn_state)

    return RedirectResponse(google_auth_url)


@app.post("/token")
async def token_endpoint(request: Request):
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    data = connector_auth.redeem_auth_code(
        form.get("code"), form.get("code_verifier"), form.get("redirect_uri")
    )
    if not data:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    return JSONResponse(connector_auth.issue_access_token(data["google_email"]))


@app.get("/connector/status")
async def connector_status():
    return {
        "status": "connected",
        "name": "Gmail Email MCP",
        "version": "1.0",
        "endpoints": {"mcp": "/mcp", "emails": "/emails", "health": "/health"},
    }


@app.get("/mcp/info")
async def mcp_info():
    base = settings.public_base_url.rstrip("/")
    return {
        "name": "Gmail Email MCP",
        "version": "1.0",
        "status": "ready",
        "url": f"{base}/mcp",
        "capabilities": {
            "tools": [
                "search_emails", "get_email", "list_emails", "get_thread",
                "search_by_sender", "search_by_subject", "search_by_date",
            ]
        },
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/")
def root():
    base = settings.public_base_url.rstrip("/")
    return {
        "service": "Gmail Email MCP",
        "version": "1.0",
        "status": "running",
        "mcp_endpoint": f"{base}/mcp",
        "endpoints": {
            "health": "/health",
            "authorize": "/authorize",
            "token": "/token",
            "register": "/register",
            "connector_status": "/connector/status",
            "mcp": "/mcp",
            "mcp_info": "/mcp/info",
            "emails": "/emails",
            "auth": "/auth/google",
        },
    }
