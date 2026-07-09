"""
Google Drive (re-)authorization.

Run this any time the app's Drive uploads are disabled — missing token.json,
corrupted token.json, or an invalid/revoked refresh token — as well as for
the very first setup. Opens a browser for you to log in and consent, and
always overwrites token.json with a fresh one.

While the OAuth consent screen is in Testing (not published to production),
Google expires refresh tokens after ~7 days — expect to need to re-run this
periodically until the app is published. drive_uploader.py already detects
every one of these broken states automatically and disables uploads safely
without crashing anything; this script is simply how you fix it when it does.

Prerequisites (see CLAUDE.md "Team Reports" section):
  1. A Google Cloud project with the Drive API enabled.
  2. An OAuth 2.0 Client ID (type: Desktop app), downloaded as credentials.json
     in this project's root (or point GOOGLE_DRIVE_CREDENTIALS_PATH at it).
  3. The Google account you log in with below must be added as a "Test user"
     on the OAuth consent screen while the app is in Testing mode, or Google
     will block the login.

Usage:
    python scripts/authorize_drive.py

Produces token.json in this project's root (or GOOGLE_DRIVE_TOKEN_PATH).
token.json is a secret tied to the authorized Google account — never commit it.
"""
import os
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    credentials_path = os.getenv("GOOGLE_DRIVE_CREDENTIALS_PATH", "credentials.json")
    token_path = os.getenv("GOOGLE_DRIVE_TOKEN_PATH", "token.json")

    if not os.path.exists(credentials_path):
        raise SystemExit(
            f"credentials.json not found at {credentials_path!r}. "
            "Download it from Google Cloud Console (OAuth 2.0 Client ID, Desktop app type) first."
        )

    flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
    # prompt="consent" forces Google to always hand back a refresh token, even if
    # this account already authorized the app before — without it, a re-run after
    # a revoked/expired token could silently come back with no refresh token at all.
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    with open(token_path, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print(f"Authorized. Token saved to {token_path}")


if __name__ == "__main__":
    main()
