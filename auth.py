import os
import ssl

from google.oauth2 import service_account

from config import SERVICE_ACCOUNT_FILE, SCOPES

# On corporate networks (e.g. Zscaler), the proxy re-signs TLS traffic with its
# own CA. truststore makes Python use the OS certificate store (Windows/macOS/Linux)
# which already has the corporate CA installed by IT — so connections succeed.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass  # not installed; default SSL behaviour


def get_credentials():
    """Return service-account credentials — no browser, no token file."""
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise FileNotFoundError(
            f"Service account key not found: {SERVICE_ACCOUNT_FILE}\n"
            "Ensure the JSON key file is present in the secrets/ folder."
        )
    return service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
