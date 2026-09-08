from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
 model_config=SettingsConfigDict(env_file=".env",extra="ignore")
 app_name:str="Gmail Email MCP"; environment:str="development"; database_url:str
 public_base_url:str; google_client_secrets_file:str="credentials.json"; google_oauth_redirect_uri:str
 google_pubsub_topic:str; google_pubsub_audience:str; token_encryption_key:str; mcp_api_key:str; watch_renewal_days:int=6
settings=Settings()
