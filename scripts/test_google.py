import json
import webbrowser
import os
from pathlib import Path
from openjarvis.connectors.oauth import build_google_auth_url, GOOGLE_ALL_SCOPES

def test_google():
    creds_path = Path("~/.openjarvis/connectors/gcalendar.json").expanduser()
    if not creds_path.exists():
        print(f"ERROR: Credentials not found at {creds_path}")
        return

    with open(creds_path, "r") as f:
        creds = json.load(f)

    client_id = creds.get("client_id")
    client_secret = creds.get("client_secret")

    if not client_id or not client_secret:
        print("ERROR: Missing client_id or client_secret in credentials file.")
        return

    print(f"Testing with Client ID: {client_id[:20]}...")
    
    # We use a standard redirect URI for testing
    redirect_uri = "http://localhost:8789/callback"
    
    try:
        url = build_google_auth_url(
            client_id=client_id,
            scopes=GOOGLE_ALL_SCOPES,
            redirect_uri=redirect_uri
        )
        print(f"\nGenerated Auth URL:\n{url}\n")
        print("Opening browser...")
        webbrowser.open(url)
        print("\nSUCCESS: If a browser tab opened, your credentials are valid.")
        print("Please follow the steps in the browser to finish authorization.")
    except Exception as e:
        print(f"ERROR: Failed to generate or open URL: {e}")

if __name__ == "__main__":
    test_google()
