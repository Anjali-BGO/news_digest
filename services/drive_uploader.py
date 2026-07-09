import io
import json
import os
from dotenv import load_dotenv
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from logger import get_logger

load_dotenv()

log = get_logger("drive_uploader")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

_service = None
_last_warning_key = None

_AUTHORIZE_HINT = "Run `python scripts/authorize_drive.py` to (re)authorize Drive access."


def _warn_once(key: str, message: str) -> None:
    """Log a given failure reason once, not on every single upload attempt."""
    global _last_warning_key
    if _last_warning_key != key:
        log.warning(message)
        _last_warning_key = key


def _token_path() -> str:
    return os.getenv("GOOGLE_DRIVE_TOKEN_PATH", "token.json")


def _quarantine_token(path: str) -> None:
    """
    Move a bad token file aside so a stale/corrupt/revoked token doesn't keep
    tripping the same error on every call, and so it's recoverable for
    debugging rather than silently lost. Mirrors storage.py's corrupt-file
    backup pattern for the same reason.
    """
    try:
        bad_path = f"{path}.invalid"
        os.replace(path, bad_path)  # replace() works even if bad_path already exists on Windows
        log.warning(f"Moved unusable Drive token to {bad_path}")
    except Exception as e:
        log.error(f"Could not quarantine unusable Drive token {path}: {e}")


def _get_service():
    """
    Resolve a Drive service handle, transparently handling every token state
    a testing-mode OAuth app (not yet published to production) can hit:
      - valid token                          -> used as-is
      - expired access token + valid refresh  -> refreshed transparently, persisted
      - invalid/revoked refresh token          -> token quarantined, uploads disabled until re-authorized
      - corrupted token.json                   -> token quarantined, uploads disabled until re-authorized
      - missing token.json                     -> uploads disabled until authorized

    Testing-mode consent screens get refresh tokens that Google expires after
    ~7 days — expect the "invalid/revoked" path to trigger periodically until
    the app is published to production. The fix in every disabled case is the
    same: run scripts/authorize_drive.py again.
    """
    global _service, _last_warning_key
    if _service is not None:
        return _service

    token_path = _token_path()

    if not os.path.exists(token_path):
        _warn_once("missing", f"Drive token not found at {token_path} — Drive uploads disabled. {_AUTHORIZE_HINT}")
        return None

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except (ValueError, json.JSONDecodeError, KeyError) as e:
        log.error(f"Drive token at {token_path} is corrupted ({e}) — Drive uploads disabled. {_AUTHORIZE_HINT}")
        _quarantine_token(token_path)
        return None

    if creds.valid:
        _service = build("drive", "v3", credentials=creds)
        _last_warning_key = None  # clear any prior warning now that we're healthy again
        return _service

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            log.error(
                f"Drive refresh token is invalid or revoked ({e}) — Drive uploads disabled. "
                f"This is expected roughly every 7 days for a testing-mode OAuth app. {_AUTHORIZE_HINT}"
            )
            _quarantine_token(token_path)
            return None
        with open(token_path, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
        log.info("Drive access token refreshed")
        _service = build("drive", "v3", credentials=creds)
        _last_warning_key = None
        return _service

    # Expired with no refresh token available at all — same remedy as any other bad token.
    log.error(f"Drive token at {token_path} is expired with no refresh token — Drive uploads disabled. {_AUTHORIZE_HINT}")
    _quarantine_token(token_path)
    return None


def startup_check() -> None:
    """
    Run once when the app starts so the current Drive token status is obvious
    immediately in the logs, instead of only surfacing on the first pipeline
    run or export. _get_service() already logs the specific reason when
    uploads aren't ready; this just confirms the happy path too.
    """
    if _get_service():
        log.info("Drive uploads: ready (token valid)")


def upload_bytes(filename: str, content: bytes, mime_type: str, folder_id: str) -> None:
    """Create a new file in Drive. Fails soft — never breaks the caller's primary action."""
    if not folder_id:
        return
    try:
        service = _get_service()
        if not service:
            return
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        service.files().create(body={"name": filename, "parents": [folder_id]}, media_body=media).execute()
    except Exception as e:
        log.error(f"Drive upload failed [{filename}]: {e}")


def upsert_bytes(filename: str, content: bytes, mime_type: str, folder_id: str) -> None:
    """
    Update the file in place if one with this exact name already exists in the
    folder, otherwise create it. Used for each person's full state files and logs,
    which are re-uploaded after every run — upsert keeps Drive holding one current,
    complete copy per person instead of accumulating a new file every run.
    """
    if not folder_id:
        return
    try:
        service = _get_service()
        if not service:
            return
        q = f"name = '{filename}' and '{folder_id}' in parents and trashed = false"
        existing = service.files().list(q=q, fields="files(id)").execute().get("files", [])
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        if existing:
            service.files().update(fileId=existing[0]["id"], media_body=media).execute()
        else:
            service.files().create(body={"name": filename, "parents": [folder_id]}, media_body=media).execute()
    except Exception as e:
        log.error(f"Drive upsert failed [{filename}]: {e}")
