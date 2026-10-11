import sys
import json
import urllib.request
import urllib.parse

auth_code = sys.argv[1].strip()

with open('credentials.json') as f:
    client_info = json.load(f)['installed']

client_id = client_info['client_id']
client_secret = client_info['client_secret']
token_uri = client_info.get('token_uri', 'https://oauth2.googleapis.com/token')

data = urllib.parse.urlencode({
    'code': auth_code,
    'client_id': client_id,
    'client_secret': client_secret,
    'redirect_uri': 'http://localhost:8080/',
    'grant_type': 'authorization_code'
}).encode('utf-8')

req = urllib.request.Request(token_uri, data=data, method='POST')
try:
    with urllib.request.urlopen(req) as resp:
        res = json.loads(resp.read().decode('utf-8'))
        
        token_data = {
            'token': res.get('access_token'),
            'refresh_token': res.get('refresh_token'),
            'token_uri': token_uri,
            'client_id': client_id,
            'client_secret': client_secret,
            'scopes': ['https://www.googleapis.com/auth/calendar']
        }
        with open('token.json', 'w') as tf:
            json.dump(token_data, tf, indent=2)
        print('SUCCESS_SAVED_TOKEN')
except urllib.error.HTTPError as e:
    print('ERROR:', e.read().decode())
