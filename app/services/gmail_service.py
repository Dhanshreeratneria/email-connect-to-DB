from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
def gmail(credentials:Credentials): return build("gmail","v1",credentials=credentials,cache_discovery=False)
def profile(service): return service.users().getProfile(userId="me").execute()
def get_message(service,message_id:str): return service.users().messages().get(userId="me",id=message_id,format="full").execute()
def list_messages(service,page_token=None): return service.users().messages().list(userId="me",maxResults=500,pageToken=page_token).execute()
def history(service,start_history_id:str): return service.users().history().list(userId="me",startHistoryId=start_history_id,historyTypes=["messageAdded"]).execute()
def watch(service,topic:str): return service.users().watch(userId="me",body={"topicName":topic,"labelIds":["INBOX"]}).execute()
