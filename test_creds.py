from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/calendar']
try:
    flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
    print("FLOW_READY")
except Exception as e:
    print(f"ERROR: {e}")
