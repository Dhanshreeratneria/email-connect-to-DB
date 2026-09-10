from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import Session

from app.api.auth import router as auth_router
from app.api.webhook import router as webhook_router
from app.api.emails import router as emails_router
from app.config import settings
from app.database import get_db
from app.mcp.server import mcp
from app.services import connector_auth
from app.services.oauth_service import authorization_url

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for MCP session"""
    logger.info("Starting Gmail Email MCP application")
    async with mcp.session_manager.run():
        yield
    logger.info("Shutting down Gmail Email MCP application")


# Initialize FastAPI app
app = FastAPI(
    title="Gmail Email MCP",
    description="Connect your Gmail to Claude.ai via MCP (Model Context Protocol)",
    version="1.0",
    lifespan=lifespan
)

# Configure CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(emails_router)

# Configure MCP server
logger.info("Configuring MCP server")
mcp.settings.streamable_http_path = "/"
mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=["email-connect-to-db.onrender.com"],
    allowed_origins=["*"],
)


class RequireBearerToken:
    """
    ASGI middleware that wraps the MCP app.
    Validates Bearer tokens before allowing access to the MCP endpoint.
    """

    def __init__(self, inner_app):
        self.inner_app = inner_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.inner_app(scope, receive, send)
            return

        # Extract Authorization header
        headers = dict(scope.get("headers") or [])
        auth_header = headers.get(b"authorization", b"").decode()
        token = auth_header[7:] if auth_header.lower().startswith("bearer ") else None
        
        # Verify token
        token_data = connector_auth.verify_access_token(token) if token else None

        if not token_data:
            base = settings.public_base_url.rstrip("/")
            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "error_description": "Missing or invalid access token"
                },
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


# Mount MCP endpoint with Bearer token protection
app.mount("/mcp", RequireBearerToken(mcp.streamable_http_app()), name="mcp")


# ==================== PUBLIC ENDPOINTS ====================

@app.get("/health")
def health():
    """Health check endpoint"""
    return {"status": "ok"}


@app.get("/")
def root():
    """Root endpoint - service information"""
    base = settings.public_base_url.rstrip("/")
    return {
        "service": "Gmail Email MCP",
        "version": "1.0",
        "status": "running",
        "description": "Connect your Gmail to Claude.ai via MCP (Model Context Protocol)",
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


@app.get("/connector/status")
def connector_status():
    """Connector status endpoint"""
    return {
        "status": "connected",
        "name": "Gmail Email MCP",
        "version": "1.0",
        "endpoints": {
            "mcp": "/mcp",
            "emails": "/emails",
            "health": "/health"
        },
    }


@app.get("/mcp/info")
def mcp_info():
    """MCP capabilities and tools information"""
    base = settings.public_base_url.rstrip("/")
    return {
        "name": "Gmail Email MCP",
        "version": "1.0",
        "status": "ready",
        "url": f"{base}/mcp",
        "capabilities": {
            "tools": [
                "search_emails",
                "get_email",
                "list_emails",
                "get_thread",
                "search_by_sender",
                "search_by_subject",
                "search_by_date",
            ]
        },
    }


# ==================== OAUTH ENDPOINTS ====================

@app.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata():
    """OAuth authorization server metadata endpoint"""
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


@app.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata():
    """Protected resource metadata endpoint"""
    base = settings.public_base_url.rstrip("/")
    return {
        "resource": f"{base}/mcp",
        "authorization_servers": [base]
    }


@app.post("/register")
async def register_client(request: Request):
    """
    OAuth client registration endpoint.
    Registers a new OAuth client and returns client_id.
    """
    try:
        body = await request.json()
        redirect_uris = body.get("redirect_uris", [])
        
        if not redirect_uris:
            return JSONResponse(
                {"error": "invalid_request", "error_description": "redirect_uris required"},
                status_code=400
            )
        
        client_id = connector_auth.register_client(redirect_uris)
        logger.info(f"Registered new OAuth client: {client_id}")
        
        return JSONResponse({
            "client_id": client_id,
            "redirect_uris": redirect_uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
        })
    except Exception as e:
        logger.exception("Client registration error")
        return JSONResponse(
            {"error": "server_error", "error_description": str(e)},
            status_code=500
        )


@app.get("/authorize")
@app.post("/authorize")
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
    """
    OAuth authorization endpoint.
    Bounces the browser through real Google login.
    Supports both GET and POST requests.
    
    Only the mailbox owner (checked in /auth/google/callback) gets a code.
    """
    logger.info(f"Authorization request - client_id: {client_id}, redirect_uri: {redirect_uri}")
    
    if response_type != "code" or not redirect_uri or not code_challenge:
        logger.warning(f"Invalid authorization request - response_type: {response_type}, redirect_uri: {redirect_uri}, code_challenge: {code_challenge}")
        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": "Missing required parameters: response_type=code, redirect_uri, code_challenge"
            },
            status_code=400
        )

    try:
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

        logger.info(f"Redirecting to Google OAuth - state: {conn_state}")
        return RedirectResponse(google_auth_url)
    
    except Exception as e:
        logger.exception("Authorization endpoint error")
        return JSONResponse(
            {"error": "server_error", "error_description": str(e)},
            status_code=500
        )


@app.post("/token")
@app.get("/token")
async def token_endpoint(request: Request):
    """
    OAuth token exchange endpoint.
    Exchanges authorization code for access token.
    Supports both GET and POST requests.
    
    Required parameters:
    - grant_type: "authorization_code"
    - code: Authorization code from /authorize
    - code_verifier: PKCE code verifier
    - redirect_uri: Must match the redirect_uri from /authorize
    """
    logger.info(f"Token endpoint - method: {request.method}")
    
    try:
        # Handle both GET query params and POST form data
        if request.method == "POST":
            form = await request.form()
        else:  # GET
            form = request.query_params
        
        grant_type = form.get("grant_type")
        code = form.get("code")
        code_verifier = form.get("code_verifier")
        redirect_uri = form.get("redirect_uri")
        
        logger.info(f"Token request - grant_type: {grant_type}, code: {code[:10] if code else None}...")
        
        if grant_type != "authorization_code":
            logger.warning(f"Invalid grant type: {grant_type}")
            return JSONResponse(
                {"error": "unsupported_grant_type"},
                status_code=400
            )

        data = connector_auth.redeem_auth_code(code, code_verifier, redirect_uri)
        if not data:
            logger.warning(f"Failed to redeem auth code: {code}")
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "Invalid authorization code"},
                status_code=400
            )

        token_response = connector_auth.issue_access_token(data["google_email"])
        logger.info(f"Token issued for: {data['google_email']}")
        return JSONResponse(token_response)
    
    except Exception as e:
        logger.exception("Token endpoint error")
        return JSONResponse(
            {"error": "server_error", "error_description": str(e)},
            status_code=500
        )


# ==================== ERROR HANDLERS ====================

@app.exception_handler(404)
async def not_found_exception_handler(request: Request, exc):
    """Handle 404 Not Found errors"""
    return JSONResponse(
        {"error": "not_found", "message": f"Endpoint {request.url.path} not found"},
        status_code=404
    )


@app.exception_handler(500)
async def internal_server_error_handler(request: Request, exc):
    """Handle 500 Internal Server Error"""
    logger.exception("Unhandled exception in request")
    return JSONResponse(
        {"error": "internal_server_error", "message": "An internal error occurred"},
        status_code=500
    )


# ==================== STARTUP ====================

@app.on_event("startup")
async def startup_event():
    """Startup event - log application initialization"""
    logger.info("=" * 50)
    logger.info("Gmail Email MCP Starting Up")
    logger.info("=" * 50)
    logger.info(f"Environment: {settings.environment}")
    logger.info(f"Public Base URL: {settings.public_base_url}")
    logger.info(f"Database: {settings.database_url.split('@')[1] if '@' in settings.database_url else 'configured'}")
    logger.info(f"MCP Endpoint: {settings.public_base_url}/mcp")
    logger.info("=" * 50)


@app.on_event("shutdown")
async def shutdown_event():
    """Shutdown event - log application shutdown"""
    logger.info("Gmail Email MCP Shutting Down")


# ==================== DEBUGGING ====================

if __name__ == "__main__":
    import uvicorn
    
    logger.info("Starting Gmail Email MCP server")
    logger.info(f"Listening on http://0.0.0.0:8000")
    logger.info(f"API Docs: http://0.0.0.0:8000/docs")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )