import os
from pathlib import Path

import msal
import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

TENANT = os.getenv("GRAPH_TENANT", "consumers")
CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
SCOPES = os.getenv("GRAPH_SCOPES", "Files.ReadWrite.All offline_access").split()
INBOX = os.getenv("ONEDRIVE_INBOX", "vomDrucker")

cache_path = BASE / ".token_cache.bin"
cache = msal.SerializableTokenCache()
if cache_path.exists():
    cache.deserialize(cache_path.read_text())

app = msal.PublicClientApplication(
    client_id=CLIENT_ID,
    authority=f"https://login.microsoftonline.com/{TENANT}",
    token_cache=cache,
)
accounts = app.get_accounts()
if not accounts:
    raise SystemExit("No cached account. Run auth_device_code.py first.")

result = app.acquire_token_silent(SCOPES, account=accounts[0])
if not result or "access_token" not in result:
    raise SystemExit(f"No token in cache (or expired). Run auth_device_code.py. Got: {result}")

headers = {"Authorization": f"Bearer {result['access_token']}"}
# list children of a folder by path
url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{INBOX}:/children?$select=id,name,createdDateTime,lastModifiedDateTime,file"
res = requests.get(url, headers=headers, timeout=30)
res.raise_for_status()
items = res.json().get("value", [])

pdfs = [it for it in items if it.get('file') and it['name'].lower().endswith('.pdf')]
print(f"Inbox '{INBOX}': {len(items)} items ({len(pdfs)} pdfs)")
for it in pdfs:
    print(f"- {it['name']}  created={it.get('createdDateTime')}  id={it.get('id')}")
