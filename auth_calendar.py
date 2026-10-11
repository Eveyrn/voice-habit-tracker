import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/calendar']
CREDENTIALS_FILE = 'credentials.json'
TOKEN_FILE = 'token.json'

flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
# open_browser=True ??????? ????????? ??????? Windows
creds = flow.run_local_server(port=8080, open_browser=True)

with open(TOKEN_FILE, 'w') as token:
    token.write(creds.to_json())

print('SUCCESS_AUTH')
