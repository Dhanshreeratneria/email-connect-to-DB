import json
import base64
import secrets
import hashlib
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from app.config import settings

# In-memory storage for OAuth flows (during single browser session)
# Maps state -> (flow, code_verifier) tuple
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
    
    Manually handles PKCE to ensure code_verifier is preserved.
    
    Returns:
        tuple: (authorization_url, state)
    """
    flow = get_flow()
    
    # Generate PKCE code_verifier and code_challenge
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip('=')
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip('=')
    
    # Generate authorization URL with PKCE
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        code_challenge=code_challenge,
        code_challenge_method='S256'
    )
    
    # Store the flow instance AND code_verifier for later use
    _oauth_flows[state] = {
        'flow': flow,
        'code_verifier': code_verifier,
        'code_challenge': code_challenge
    }
    
    print(f"[DEBUG] Authorization URL generated with state: {state}")
    print(f"[DEBUG] Code verifier stored for PKCE")
    
    return auth_url, state


def exchange(code: str, state: str = None) -> Credentials:
    """
    Exchange authorization code for credentials.
    
    Uses the stored Flow instance and code_verifier for PKCE.
    
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
        
        # Get the stored flow and code_verifier using state
        if state and state in _oauth_flows:
            oauth_data = _oauth_flows.pop(state)  # Remove to prevent reuse
            flow = oauth_data['flow']
            code_verifier = oauth_data['code_verifier']
            print(f"[DEBUG] Found stored flow and code_verifier for state: {state}")
        else:
            # Fallback: create new flow if state not found
            print("[DEBUG] State not in storage, creating new flow (PKCE may fail)...")
            flow = get_flow()
            code_verifier = None
        
        # Fetch token using the code and code_verifier for PKCE validation
        print(f"[DEBUG] Fetching token with code_verifier...")
        if code_verifier:
            flow.fetch_token(code=code, code_verifier=code_verifier)
        else:
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