import os
import pickle
import ssl

from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

from config import SCOPES, CREDS_FILE, TOKEN_FILE

# On corporate networks (e.g. Zscaler), inject the OS certificate store so
# the proxy's self-signed CA is trusted.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass


def get_credentials():
    """
    Return OAuth2 user credentials — acts as YOU, not as a robot account.

    Why OAuth instead of a service account
    ───────────────────────────────────────
    Service accounts have zero Google Drive storage quota.  They can read and
    update files you share with them, but cannot CREATE new files on personal
    Drive (storageQuotaExceeded).  OAuth credentials act as your own Google
    account so new files are created under your quota — which is what you want.

    Browser requirement
    ───────────────────
    The browser opens exactly ONCE on the very first run.  The resulting token
    (including a long-lived refresh token) is saved to TOKEN_FILE and reused on
    every subsequent run — no browser ever again unless you delete the token.
    """
    creds = None

    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as fh:
            creds = pickle.load(fh)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            print("🔄 Refreshing authentication token...")
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDS_FILE):
                raise FileNotFoundError(
                    f"OAuth credentials file not found: {CREDS_FILE}\n"
                    "Download it from Google Cloud Console → APIs & Services → Credentials\n"
                    "and place it in the secrets/ folder."
                )
            print("🔐 Opening browser for one-time authentication...")
            flow  = InstalledAppFlow.from_client_secrets_file(CREDS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)

        with open(TOKEN_FILE, 'wb') as fh:
            pickle.dump(creds, fh)
        print("✅ Authentication successful — token saved for future runs.")

    return creds
