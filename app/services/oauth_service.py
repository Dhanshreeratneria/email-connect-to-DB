import json
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from app.config import settings

# In-memory storage for OAuth flows (during single browser session)
# Maps state -> Flow instance
_oauth_flows = {}


def cipher():
    """Get cipher for token encryption"""
    return Fernet(settings.token_encryption_key.encode())


def get_flow():
    """Create and return a Flow instance with PKCE enabled"""
    flow = Flow.from_client_secrets_file(
        settings.google_client_secrets_file,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri=settings.google_oauth_redirect_uri,
    )
    return flow


def authorization_url():
    """
    Start Google OAuth flow and return authorization URL.
    
    Flow automatically handles PKCE (Proof Key for Code Exchange).
    
    Returns:
        tuple: (authorization_url, state)
    """
    flow = get_flow()
    
    # Generate authorization URL
    # Flow automatically enables PKCE by default
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent"
    )
    
    # Store the flow instance for later use
    _oauth_flows[state] = flow
    
    print(f"[DEBUG] Authorization URL generated with state: {state}")
    print(f"[DEBUG] Stored flow for state: {state}")
    
    return auth_url, state


def exchange(code: str, state: str = None) -> Credentials:
    """
    Exchange authorization code for credentials.
    
    Uses the stored Flow instance to properly handle PKCE and state.
    
    Args:
        code: Authorization code from Google
        state: State parameter for validation
        
    Returns:
        Credentials: Google OAuth credentials
        
    Raises:
        ValueError: If state is invalid or code exchange fails
    """
    try:
        print(f"[DEBUG] Exchanging code: {code[:20]}...")
        print(f"[DEBUG] State received: {state[:20] if state else 'None'}...")
        
        # Get the stored flow using state
        if state and state in _oauth_flows:
            flow = _oauth_flows.pop(state)  # Remove to prevent reuse
            print(f"[DEBUG] Found stored flow for state: {state}")
        else:
            # Fallback: create new flow if state not found
            # This handles cases where state validation is skipped
            print("[DEBUG] State not in storage, creating new flow...")
            flow = get_flow()
        
        # Fetch token using the same flow instance
        # This ensures PKCE state is properly maintained
        flow.fetch_token(code=code)
        
        print("[DEBUG] Token exchange successful!")
        
        return flow.credentials
        
    except Exception as exc:
        error_msg = str(exc)
        print(f"[DEBUG] Token exchange failed: {error_msg}")
        raise ValueError(f"OAuth token exchange failed: {error_msg}") from exc


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