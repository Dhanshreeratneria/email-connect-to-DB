from contextlib import asynccontextmanager
import hmac
import logging

from fastapi import FastAPI, Request, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.orm import Session

from app.api.auth import router as auth_router
from app.api.webhook import router as webhook_router
from app.api.emails import router as emails_router
from app.api.attachments import router as attachments_router
from app.api.admin import router as admin_router
from app.config import settings
from app.database import SessionLocal, get_db
from app.mcp.server import mcp
from app.services import admin_auth, auth0, connector_auth
from app.services.oauth_service import authorization_url


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
# APPLICATION LIFESPAN
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for MCP session."""
    logger.info("Starting Gmail Email MCP application")

    async with mcp.session_manager.run():
        yield

    logger.info("Shutting down Gmail Email MCP application")


# ============================================================
# FASTAPI APPLICATION
# ============================================================

app = FastAPI(
    title="Gmail Email MCP",
    description="Connect your Gmail to Claude.ai via MCP (Model Context Protocol)",
    version="1.0",
    lifespan=lifespan,
)


# ============================================================
# MCP PATH NORMALIZATION
# ============================================================

class NormalizeMcpPath:
    """
    Accept both:
        /mcp
        /mcp/

    Claude may call either form.
    """

    def __init__(self, inner_app):
        self.inner_app = inner_app

    async def __call__(self, scope, receive, send):

        if (
            scope["type"] == "http"
            and scope.get("path") == "/mcp"
        ):
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"

        await self.inner_app(scope, receive, send)


app.add_middleware(NormalizeMcpPath)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# ROUTERS
# ============================================================

app.include_router(auth_router)
app.include_router(webhook_router)
app.include_router(emails_router)
app.include_router(attachments_router)
app.include_router(admin_router)


# ============================================================
# MCP SERVER CONFIGURATION
# ============================================================

logger.info("Configuring MCP server")

mcp.settings.streamable_http_path = "/"

mcp.settings.transport_security = TransportSecuritySettings(
    allowed_hosts=[
        "email-connect-to-db.onrender.com"
    ],
    allowed_origins=["*"],
)


# ============================================================
# MCP BEARER TOKEN AUTHENTICATION
# ============================================================

class RequireBearerToken:
    """
    Protects the MCP endpoint.

    Supported authentication methods:

    1. MCP_API_KEY
       Static bearer credential configured in environment.

    2. Database-backed mcp_ token
       Token generated from the Admin dashboard.

    3. Existing Auth0 JWT
       Keeps the existing OAuth/Auth0 flow available.
    """

    def __init__(self, inner_app):
        self.inner_app = inner_app

    # --------------------------------------------------------
    # Extract Bearer token
    # --------------------------------------------------------

    @staticmethod
    def _extract_token(
        headers: dict[bytes, bytes],
    ) -> str | None:

        auth_header = (
            headers
            .get(b"authorization", b"")
            .decode("latin-1")
            .strip()
        )

        # No Authorization header
        if not auth_header:
            logger.info(
                "MCP authorization header present=False"
            )
            return None

        logger.info(
            "MCP authorization header present=True"
        )

        # Expected format:
        #
        # Authorization: Bearer mcp_xxxxxxxxx
        #
        scheme, separator, credentials = (
            auth_header.partition(" ")
        )

        # Reject malformed authorization header
        if not separator:
            logger.info(
                "MCP authorization header rejected: "
                "missing separator"
            )
            return None

        # Authorization scheme must be Bearer
        if scheme.lower() != "bearer":
            logger.info(
                "MCP authorization scheme rejected: %s",
                scheme[:30],
            )
            return None

        token = credentials.strip()

        if not token:
            logger.info(
                "MCP bearer credential is empty"
            )
            return None

        # ----------------------------------------------------
        # SAFE LOGGING
        # Never print the actual token.
        # ----------------------------------------------------

        logger.info(
            "MCP credential prefix=%s length=%d",
            token[:4],
            len(token),
        )

        return token

    # --------------------------------------------------------
    # ASGI request handler
    # --------------------------------------------------------

    async def __call__(
        self,
        scope,
        receive,
        send,
    ):

        # MCP authentication is only required for HTTP.
        if scope["type"] != "http":
            await self.inner_app(
                scope,
                receive,
                send,
            )
            return

        # Convert headers to dictionary
        headers = dict(
            scope.get("headers") or []
        )

        # Extract bearer credential
        token = self._extract_token(headers)

        token_data = None

        # ====================================================
        # 1. STATIC MCP_API_KEY
        # ====================================================

        if (
            token
            and settings.mcp_api_key
            and hmac.compare_digest(
                token,
                settings.mcp_api_key,
            )
        ):

            logger.info(
                "MCP authentication: "
                "static MCP_API_KEY accepted"
            )

            token_data = {
                "sub": "mcp-api-key",
                "scope": (
                    "read:emails "
                    "read:attachments "
                    "download:attachments"
                ),
            }

        # ====================================================
        # 2. DATABASE mcp_ TOKEN
        # ====================================================

        if token_data is None and token:

            with SessionLocal() as database:

                token_data = (
                    admin_auth.validate_token(
                        database,
                        token,
                    )
                )

            if token_data:

                logger.info(
                    "MCP authentication: "
                    "database token accepted "
                    "client_id=%s",
                    token_data.get("client_id"),
                )

        # ====================================================
        # 3. EXISTING AUTH0 TOKEN
        # ====================================================

        if token_data is None and token:

            token_data = (
                auth0.safe_validate_token(
                    token
                )
            )

            if token_data:

                logger.info(
                    "MCP authentication: "
                    "Auth0 token accepted"
                )

        # ====================================================
        # 4. INVALID / MISSING TOKEN
        # ====================================================

        if not token_data:

            logger.warning(
                "MCP authentication failed: "
                "no valid credential"
            )

            base = (
                settings.public_base_url
                .rstrip("/")
            )

            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "error_description": (
                        "Missing or invalid access token"
                    ),
                },
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        f'Bearer realm="mcp", '
                        f'resource_metadata='
                        f'"{base}/.well-known/'
                        f'oauth-protected-resource"'
                    )
                },
            )

            response.headers.update(
                auth0.unauthorized_response_headers(
                    base
                )
            )

            await response(
                scope,
                receive,
                send,
            )

            return

        # ====================================================
        # AUTHENTICATED REQUEST
        # ====================================================

        claims_token = (
            auth0.set_claims(
                token_data
            )
        )

        try:

            await self.inner_app(
                scope,
                receive,
                send,
            )

        finally:

            auth0.reset_claims(
                claims_token
            )


# ============================================================
# MOUNT MCP ENDPOINT
# ============================================================

app.mount(
    "/mcp",
    RequireBearerToken(
        mcp.streamable_http_app()
    ),
    name="mcp",
)


# ============================================================
# PUBLIC ENDPOINTS
# ============================================================

@app.get("/health")
def health():
    """Health check endpoint."""

    return {
        "status": "ok"
    }


@app.get("/")
def root():
    """Service information."""

    base = (
        settings.public_base_url
        .rstrip("/")
    )

    return {
        "service": "Gmail Email MCP",
        "version": "1.0",
        "status": "running",
        "description": (
            "Connect your Gmail to Claude.ai "
            "via MCP (Model Context Protocol)"
        ),
        "mcp_endpoint": (
            f"{base}/mcp/"
        ),
        "endpoints": {
            "health": "/health",
            "authorize": "/authorize",
            "token": "/token",
            "register": "/register",
            "connector_status": "/connector/status",
            "mcp": "/mcp/",
            "mcp_info": "/mcp/info",
            "emails": "/emails",
            "auth": "/auth/google",
        },
    }


@app.get("/connector/status")
def connector_status():
    """Connector status endpoint."""

    return {
        "status": "connected",
        "name": "Gmail Email MCP",
        "version": "1.0",
        "endpoints": {
            "mcp": "/mcp/",
            "emails": "/emails",
            "health": "/health",
        },
    }


@app.get("/mcp/info")
def mcp_info():
    """MCP capabilities and tools information."""

    base = (
        settings.public_base_url
        .rstrip("/")
    )

    return {
        "name": "Gmail Email MCP",
        "version": "1.0",
        "status": "ready",
        "url": f"{base}/mcp/",
        "capabilities": {
            "tools": [
                "search_emails",
                "get_email",
                "list_emails",
                "get_thread",
                "search_by_sender",
                "search_by_subject",
                "search_by_date",
                "list_attachments",
                "get_attachment_content",
                "extract_attachment_text",
                "get_email_with_attachments",
                "search_emails_with_attachments",
            ],
            "oauth_scopes": [
                "read:emails",
                "read:attachments",
                "download:attachments",
            ],
        },
    }


# ============================================================
# OAUTH ENDPOINTS
# ============================================================

@app.get("/.well-known/oauth-authorization-server")
def authorization_server_metadata():
    """OAuth authorization server metadata."""

    base = (
        settings.public_base_url
        .rstrip("/")
    )

    return {
        "issuer": base,
        "authorization_endpoint": (
            f"{base}/authorize"
        ),
        "token_endpoint": (
            f"{base}/token"
        ),
        "jwks_uri": (
            f"{base}/.well-known/jwks.json"
        ),
        "response_types_supported": [
            "code"
        ],
        "grant_types_supported": [
            "authorization_code"
        ],
        "scopes_supported": [
            "read:emails",
            "read:attachments",
            "download:attachments",
        ],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "none",
        ],
    }


@app.get("/.well-known/oauth-protected-resource")
def protected_resource_metadata():
    """OAuth protected resource metadata."""

    base = (
        settings.public_base_url
        .rstrip("/")
    )

    return {
        "resource": (
            f"{base}/mcp/"
        ),
        "authorization_servers": [
            base
        ],
        "scopes_supported": [
            "read:emails",
            "read:attachments",
            "download:attachments",
        ],
        "bearer_methods_supported": [
            "header"
        ],
    }


# ============================================================
# OAUTH CLIENT REGISTRATION
# ============================================================

@app.post("/register")
async def register_client(
    request: Request,
):
    """
    OAuth client registration endpoint.
    """

    try:

        body = await request.json()

        redirect_uris = body.get(
            "redirect_uris",
            []
        )

        if not redirect_uris:

            return JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": (
                        "redirect_uris required"
                    ),
                },
                status_code=400,
            )

        client_id = (
            connector_auth.register_client(
                redirect_uris
            )
        )

        logger.info(
            "Registered new OAuth client: %s",
            client_id,
        )

        return JSONResponse(
            {
                "client_id": client_id,
                "redirect_uris": redirect_uris,
                "token_endpoint_auth_method": "none",
                "grant_types": [
                    "authorization_code"
                ],
                "response_types": [
                    "code"
                ],
            }
        )

    except Exception as e:

        logger.exception(
            "Client registration error"
        )

        return JSONResponse(
            {
                "error": "server_error",
                "error_description": str(e),
            },
            status_code=500,
        )


# ============================================================
# OAUTH AUTHORIZE
# ============================================================

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

    Redirects through Google OAuth.
    """

    logger.info(
        "Authorization request - "
        "client_id=%s redirect_uri=%s",
        client_id,
        redirect_uri,
    )

    if (
        response_type != "code"
        or not redirect_uri
        or not code_challenge
    ):

        logger.warning(
            "Invalid authorization request"
        )

        return JSONResponse(
            {
                "error": "invalid_request",
                "error_description": (
                    "Missing required parameters: "
                    "response_type=code, "
                    "redirect_uri, "
                    "code_challenge"
                ),
            },
            status_code=400,
        )

    try:

        conn_state = (
            connector_auth.start_authorization(
                client_id=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                state=state,
                resource=resource,
            )
        )

        google_auth_url, google_state = (
            authorization_url()
        )

        connector_auth.map_google_state(
            google_state,
            conn_state,
        )

        logger.info(
            "Redirecting to Google OAuth"
        )

        return RedirectResponse(
            google_auth_url
        )

    except Exception as e:

        logger.exception(
            "Authorization endpoint error"
        )

        return JSONResponse(
            {
                "error": "server_error",
                "error_description": str(e),
            },
            status_code=500,
        )


# ============================================================
# OAUTH TOKEN ENDPOINT
# ============================================================

@app.post("/token")
@app.get("/token")
async def token_endpoint(
    request: Request,
):
    """
    OAuth token exchange endpoint.
    """

    logger.info(
        "Token endpoint - method=%s",
        request.method,
    )

    try:

        if request.method == "POST":

            form = await request.form()

        else:

            form = request.query_params

        grant_type = form.get(
            "grant_type"
        )

        code = form.get(
            "code"
        )

        code_verifier = form.get(
            "code_verifier"
        )

        redirect_uri = form.get(
            "redirect_uri"
        )

        logger.info(
            "Token request - "
            "grant_type=%s code_present=%s",
            grant_type,
            bool(code),
        )

        if grant_type != "authorization_code":

            logger.warning(
                "Invalid grant type: %s",
                grant_type,
            )

            return JSONResponse(
                {
                    "error":
                        "unsupported_grant_type"
                },
                status_code=400,
            )

        data = (
            connector_auth.redeem_auth_code(
                code,
                code_verifier,
                redirect_uri,
            )
        )

        if not data:

            logger.warning(
                "Failed to redeem auth code"
            )

            return JSONResponse(
                {
                    "error": "invalid_grant",
                    "error_description": (
                        "Invalid authorization code"
                    ),
                },
                status_code=400,
            )

        token_response = (
            connector_auth.issue_access_token(
                data["google_email"]
            )
        )

        logger.info(
            "Token issued for: %s",
            data["google_email"],
        )

        return JSONResponse(
            token_response
        )

    except Exception as e:

        logger.exception(
            "Token endpoint error"
        )

        return JSONResponse(
            {
                "error": "server_error",
                "error_description": str(e),
            },
            status_code=500,
        )


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.exception_handler(404)
async def not_found_exception_handler(
    request: Request,
    exc,
):
    """Handle 404."""

    return JSONResponse(
        {
            "error": "not_found",
            "message": (
                f"Endpoint {request.url.path} "
                "not found"
            ),
        },
        status_code=404,
    )


@app.exception_handler(500)
async def internal_server_error_handler(
    request: Request,
    exc,
):
    """Handle 500."""

    logger.exception(
        "Unhandled exception in request"
    )

    return JSONResponse(
        {
            "error": "internal_server_error",
            "message": (
                "An internal error occurred"
            ),
        },
        status_code=500,
    )


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup_event():

    logger.info("=" * 50)
    logger.info(
        "Gmail Email MCP Starting Up"
    )
    logger.info("=" * 50)

    logger.info(
        "Environment: %s",
        settings.environment,
    )

    logger.info(
        "Public Base URL: %s",
        settings.public_base_url,
    )

    logger.info(
        "Database: %s",
        (
            settings.database_url.split("@")[1]
            if "@" in settings.database_url
            else "configured"
        ),
    )

    logger.info(
        "MCP Endpoint: %s/mcp/",
        settings.public_base_url.rstrip("/"),
    )

    logger.info("=" * 50)


@app.on_event("shutdown")
async def shutdown_event():

    logger.info(
        "Gmail Email MCP Shutting Down"
    )


# ============================================================
# LOCAL DEVELOPMENT
# ============================================================

if __name__ == "__main__":

    import os
    import uvicorn

    port = int(
        os.getenv(
            "PORT",
            "8000",
        )
    )

    logger.info(
        "Starting Gmail Email MCP server"
    )

    logger.info(
        "Listening on http://0.0.0.0:%s",
        port,
    )

    logger.info(
        "API Docs: http://0.0.0.0:%s/docs",
        port,
    )

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )