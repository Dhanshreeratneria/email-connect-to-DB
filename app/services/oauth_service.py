import json
from cryptography.fernet import Fernet
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from app.config import settings
SCOPES=["https://www.googleapis.com/auth/gmail.readonly"]
def cipher(): return Fernet(settings.token_encryption_key.encode())
def flow(): return Flow.from_client_secrets_file(settings.google_client_secrets_file,scopes=SCOPES,redirect_uri=settings.google_oauth_redirect_uri)
def authorization_url():
 f=flow();url,state=f.authorization_url(access_type="offline",include_granted_scopes="true",prompt="consent");return url,state
def exchange(code:str)->Credentials:
 f=flow();f.fetch_token(code=code);return f.credentials
def encrypt_credentials(c:Credentials)->str: return cipher().encrypt(c.to_json().encode()).decode()
def decrypt_credentials(value:str)->Credentials: return Credentials.from_authorized_user_info(json.loads(cipher().decrypt(value.encode()).decode()),SCOPES)
