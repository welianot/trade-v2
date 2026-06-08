"""
fyers_auth.py
=============
Run once daily to generate Fyers access_token.
Token saved to fyers_token.txt — read by fyers_data.py.

Run:
    python fyers_auth.py

Steps:
  1. Opens auth URL in browser
  2. You login + approve
  3. Browser redirects to 127.0.0.1:5000/?auth_code=XXXX
  4. Script catches auth_code, exchanges for access_token
  5. Saves token to fyers_token.txt
"""

import os
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from fyers_apiv3 import fyersModel

# ─── Load .env ───────────────────────────────────────────────────────────────

def _load_env():
    env = {}
    try:
        for line in open(".env"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env

_ENV = _load_env()
CLIENT_ID    = _ENV.get("FYERS_CLIENT_ID", "")
SECRET_KEY   = _ENV.get("FYERS_SECRET_KEY", "")
REDIRECT_URI = _ENV.get("FYERS_REDIRECT_URI", "http://127.0.0.1:5000/")
TOKEN_FILE   = "fyers_token.txt"

# ─── Local server to catch redirect ──────────────────────────────────────────

auth_code_received = None

class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        global auth_code_received
        params = parse_qs(urlparse(self.path).query)
        auth_code_received = params.get("auth_code", [None])[0]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"<h2>Auth code received! You can close this tab.</h2>")

    def log_message(self, format, *args):
        pass  # suppress server logs

# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if not CLIENT_ID or not SECRET_KEY:
        print("ERROR: FYERS_CLIENT_ID or FYERS_SECRET_KEY not set in .env")
        return

    # Step 1: Generate auth URL
    session = fyersModel.SessionModel(
        client_id=CLIENT_ID,
        secret_key=SECRET_KEY,
        redirect_uri=REDIRECT_URI,
        response_type="code",
        grant_type="authorization_code",
    )
    auth_url = session.generate_authcode()
    print(f"\nOpening browser for Fyers login...")
    print(f"URL: {auth_url}\n")
    webbrowser.open(auth_url)

    # Step 2: Start local server to catch auth_code
    print("Waiting for redirect (login in browser)...")
    server = HTTPServer(("127.0.0.1", 5000), _Handler)
    server.handle_request()  # handle ONE request then stop

    if not auth_code_received:
        print("ERROR: No auth code received.")
        return

    print(f"Auth code received: {auth_code_received[:10]}...")

    # Step 3: Exchange auth_code for access_token
    session.set_token(auth_code_received)
    response = session.generate_token()

    if response.get("s") != "ok":
        print(f"ERROR generating token: {response}")
        return

    access_token = response["access_token"]

    # Step 4: Save token
    with open(TOKEN_FILE, "w") as f:
        f.write(access_token)

    print(f"\n✅ Access token saved to {TOKEN_FILE}")
    print(f"Token: {access_token[:20]}...")
    print("\nNow run: python fyers_data.py")

if __name__ == "__main__":
    main()