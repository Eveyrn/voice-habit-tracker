from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/calendar']
REDIRECT_URI = 'http://localhost:8080/'

flow = InstalledAppFlow.from_client_secrets_file(
    'credentials.json',
    scopes=SCOPES,
    redirect_uri=REDIRECT_URI
)

auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline')

print('---CLICK_THIS_URL---')
print(auth_url)
print('---END_URL---')

code = None

class AuthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global code
        parsed_url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed_url.query)
        if 'code' in query:
            code = query['code'][0]
            self.send_response(200)
            self.send_header('Content-type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write('<h1>??????????? ???????! ?????? ??????? ??? ??????? ? ????????? ? ????.</h1>'.encode('utf-8'))
        else:
            self.send_response(400)
            self.end_headers()

server = HTTPServer(('localhost', 8080), AuthHandler)
print('SERVER_LISTENING')
while code is None:
    server.handle_request()

flow.fetch_token(code=code)
creds = flow.credentials

with open('token.json', 'w') as f:
    f.write(creds.to_json())

print('TOKEN_SAVED_SUCCESSFULLY')
