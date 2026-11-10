
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

import json
from io import BytesIO
from googleapiclient.http import MediaIoBaseUpload

BASE_DIR = Path(__file__).resolve().parent

CREDENTIALS_FILE = BASE_DIR / "credential_google.json"
TOKEN_FILE = BASE_DIR / "token.json"

SCOPES = [
    "https://www.googleapis.com/auth/drive.file"
]


def authenticate_google_drive():
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(
            str(TOKEN_FILE), SCOPES
        )

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        TOKEN_FILE.write_text(creds.to_json())
        TOKEN_FILE.chmod(0o600)

    service = build("drive", "v3", credentials=creds)
    return service

def upload_json(data: dict, filename: str, folder_id: str):
    service = authenticate_google_drive()

    json_bytes = json.dumps(
        data, indent=2, ensure_ascii=False
    ).encode("utf-8")

    media = MediaIoBaseUpload(
        BytesIO(json_bytes),
        mimetype="application/json",
        resumable=False,
    )

    metadata = {
        "name": filename,
        "mimeType": "application/json",
        "parents": [folder_id],
    }

    file = service.files().create(
        body=metadata,
        media_body=media,
        fields="id,name,webViewLink",
    ).execute()

    print(f"Uploaded: {file['name']}")
    print(f"File ID: {file['id']}")

    return file



if __name__ == "__main__":
    service = authenticate_google_drive()

    print("Google Drive authentication successful!")

    about = service.about().get(fields="user").execute()
    print("Connected account:", about["user"]["emailAddress"])
