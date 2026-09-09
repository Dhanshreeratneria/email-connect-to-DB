import json
import secrets
import base64
import hashlib
from urllib.parse import urlencode
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from app.config import settings

# State storage - maps state to PKCE verifier
_state_storage = {}


def cipher():
    """Get cipher for token encryption"""
    return Fernet(settings.token_encryption_key.encode())


def generate_pkce_pair():
    """
    Generate PKCE code_verifier and code_challenge.
    
    Returns:
        tuple: (code_verifier, code_challenge)
    """
    # Generate random code verifier (43-128 chars, unreserved characters)
    code_verifier = base64.urlsafe_b64encode(
        secrets.token_bytes(32)
    ).decode("utf-8").rstrip("=")
    
    # Generate code challenge from verifier
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode("utf-8").rstrip("=")
    
    return code_verifier, challenge


def get_flow():
    """Create and return a Flow instance"""
    flow = Flow.from_client_secrets_file(
        settings.google_client_secrets_file,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri=settings.google_oauth_redirect_uri
    )
    return flow


def authorization_url():
    """
    Start Google OAuth flow and return authorization URL.
    
    Returns:
        tuple: (authorization_url, state)
    """
    flow = get_flow()
    
    # Generate state for CSRF protection
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("utf-8").rstrip("=")
    
    # Generate PKCE
    code_verifier, code_challenge = generate_pkce_pair()
    
    # Store verifier and flow for later use
    _state_storage[state] = {
        "code_verifier": code_verifier,
        "code_challenge": code_challenge,
        "flow": flow
    }
    
    # Get authorization URL with PKCE
    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state,
        code_challenge=code_challenge,
        code_challenge_method="S256"
    )
    
    return auth_url, state


def exchange(code: str, state: str = None) -> Credentials:
    """
    Exchange authorization code for credentials.
    
    Args:
        code: Authorization code from Google
        state: State parameter for verification
        
    Returns:
        Credentials: Google OAuth credentials
        
    Raises:
        ValueError: If state is invalid or code exchange fails
    """
    # Validate state
    if not state or state not in _state_storage:
        raise ValueError("Invalid or missing state parameter")
    
    stored_data = _state_storage.pop(state)  # Remove to prevent replay
    code_verifier = stored_data["code_verifier"]
    flow = stored_data["flow"]
    
    try:
        # Fetch token with code verifier
        flow.fetch_token(
            code=code,
            code_verifier=code_verifier
        )
        return flow.credentials
    except Exception as exc:
        raise ValueError(f"Token exchange failed: {str(exc)}") from exc


def encrypt_credentials(credentials: Credentials) -> str:
    """
    Encrypt credentials for storage.
    
    Args:
        credentials: Google OAuth credentials
        
    Returns:
        str: Encrypted credentials JSON
    """
    json_creds = credentials.to_json()
    encrypted = cipher().encrypt(json_creds.encode())
    return encrypted.decode()


def decrypt_credentials(encrypted_value: str) -> Credentials:
    """
    Decrypt stored credentials.
    
    Args:
        encrypted_value: Encrypted credentials JSON
        
    Returns:
        Credentials: Decrypted Google OAuth credentials
    """
    decrypted = cipher().decrypt(encrypted_value.encode()).decode()
    creds_info = json.loads(decrypted)
    
    return Credentials.from_authorized_user_info(
        creds_info,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"]
    )