import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/calendar']
flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES, redirect_uri='urn:ietf:wg:oauth:2.0:oob')
auth_url, _ = flow.authorization_url(prompt='consent')
print('AUTH_URL_START')
print(auth_url)
print('AUTH_URL_END')
