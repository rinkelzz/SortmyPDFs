import os
from pathlib import Path

import msal
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

TENANT = os.getenv("GRAPH_TENANT", "consumers")
CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
SCOPES = os.getenv("GRAPH_SCOPES", "https://graph.microsoft.com/Files.ReadWrite.All offline_access").split()

if not CLIENT_ID:
    raise SystemExit("Missing GRAPH_CLIENT_ID. Copy .env.example -> .env and set GRAPH_CLIENT_ID.")

cache_path = BASE / ".token_cache.bin"
cache = msal.SerializableTokenCache()
if cache_path.exists():
    cache.deserialize(cache_path.read_text())

app = msal.PublicClientApplication(
    client_id=CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{TENANT}",
    token_cache=cache,
)

# Try silent first
accounts = app.get_accounts()
result = None
if accounts:
    result = app.acquire_token_silent(SCOPES, account=accounts[0])

if not result:
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        raise SystemExit(f"Failed to create device flow: {flow}")
    print(flow["message"])  # contains URL + code
    result = app.acquire_token_by_device_flow(flow)

if "access_token" not in result:
    raise SystemExit(f"Auth failed: {result}")

if cache.has_state_changed:
    cache_path.write_text(cache.serialize())

print("OK: token acquired. Expires in", result.get("expires_in"), "seconds")
