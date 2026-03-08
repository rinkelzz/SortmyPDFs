from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

# Optional (only used if OneDrive live inbox is enabled)
try:
    import msal  # type: ignore
    import requests  # type: ignore
    from dotenv import load_dotenv  # type: ignore
except Exception:  # pragma: no cover
    msal = None
    requests = None
    load_dotenv = None

BASE = Path(__file__).resolve().parents[1]  # SortmyPDFs/
DASH = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(DASH / "templates"))

STATE_PATH = BASE / "state.json"
STATE_IMAP_PATH = BASE / "state_imap.json"
LOG_DIR = BASE / "logs"

# Feature flag: enable OneDrive live query for inbox count/list
ENABLE_LIVE_INBOX = os.getenv("SORTMYPDFS_DASH_LIVE_INBOX", "0") == "1"

# Auth (recommended when binding to LAN)
DASH_USER = os.getenv("SORTMYPDFS_DASH_USER")
DASH_PASS = os.getenv("SORTMYPDFS_DASH_PASS")

# Buttons / control endpoints
ENABLE_BUTTONS = os.getenv("SORTMYPDFS_DASH_BUTTONS", "0") == "1"


@dataclass
class LogSummary:
    path: str
    ts: datetime | None
    level_counts: dict[str, int]
    error_lines: list[str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _require_auth(request: Request) -> None:
    """Very small Basic Auth gate.

    If SORTMYPDFS_DASH_USER/PASS are not set, auth is disabled.
    """

    if not DASH_USER and not DASH_PASS:
        return

    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("basic "):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})

    import base64

    try:
        raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
        user, pwd = raw.split(":", 1)
    except Exception:
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})

    if user != (DASH_USER or "") or pwd != (DASH_PASS or ""):
        raise HTTPException(status_code=401, headers={"WWW-Authenticate": "Basic"})


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _count_sonstige(processed: dict[str, Any]) -> int:
    n = 0
    for _k, v in processed.items():
        dest = str(v.get("dest", ""))
        # Expected dest like: SortmyPDFs/Sonstige/<Firma>
        if dest.startswith("SortmyPDFs/Sonstige"):
            n += 1
    return n


def _top_firms(processed: dict[str, Any], limit: int = 10) -> list[tuple[str, int]]:
    counts: dict[str, int] = {}
    for _k, v in processed.items():
        dest = str(v.get("dest", ""))
        parts = dest.split("/")
        firm = parts[2] if len(parts) >= 3 else "(unknown)"
        counts[firm] = counts.get(firm, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


LOG_TS_RE = re.compile(r"hourly-(\\d{4}-\\d{2}-\\d{2}T\\d{2}-\\d{2}-\\d{2}Z)\\.log$")


def _parse_log_ts(name: str) -> datetime | None:
    m = LOG_TS_RE.search(name)
    if not m:
        return None
    # format: 2026-03-08T11-00-49Z
    s = m.group(1).replace("-", ":", 2).replace("-", ":", 1)  # nope, not safe
    # safer manual parse:
    raw = m.group(1)
    # YYYY-MM-DDTHH-MM-SSZ
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H-%M-%SZ")
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _summarize_log(path: Path, max_error_lines: int = 20) -> LogSummary:
    text = path.read_text(encoding="utf-8", errors="replace")
    level_counts: dict[str, int] = {"ERROR": 0, "WARN": 0, "INFO": 0}
    error_lines: list[str] = []

    for line in text.splitlines():
        if "ERROR" in line:
            level_counts["ERROR"] += 1
            if len(error_lines) < max_error_lines:
                error_lines.append(line)
        elif "WARN" in line:
            level_counts["WARN"] += 1
        elif "INFO" in line:
            level_counts["INFO"] += 1

    return LogSummary(
        path=str(path.relative_to(BASE)),
        ts=_parse_log_ts(path.name),
        level_counts=level_counts,
        error_lines=error_lines,
    )


def _list_recent_logs(limit: int = 10) -> list[LogSummary]:
    if not LOG_DIR.exists():
        return []
    logs = sorted(LOG_DIR.glob("hourly-*.log"), key=lambda p: p.name, reverse=True)[:limit]
    return [_summarize_log(p) for p in logs]


def _onedrive_inbox_live(limit: int = 10) -> tuple[int | None, list[dict[str, Any]] | None, str | None]:
    """Returns (pdf_count, items, warning).

    Only works if msal/requests are installed AND token cache exists.
    """

    if not ENABLE_LIVE_INBOX:
        return None, None, "Live inbox disabled (set SORTMYPDFS_DASH_LIVE_INBOX=1 to enable)."

    if msal is None or requests is None or load_dotenv is None:
        return None, None, "Live inbox requires msal/requests/python-dotenv in the venv."

    load_dotenv(BASE / ".env")
    tenant = os.getenv("GRAPH_TENANT", "consumers")
    client_id = os.getenv("GRAPH_CLIENT_ID")
    scopes = os.getenv("GRAPH_SCOPES", "Files.ReadWrite.All offline_access").split()
    inbox = os.getenv("ONEDRIVE_INBOX", "vomDrucker")

    if not client_id:
        return None, None, "GRAPH_CLIENT_ID missing in .env; cannot query inbox live."

    cache_path = BASE / ".token_cache.bin"
    cache = msal.SerializableTokenCache()
    if cache_path.exists():
        cache.deserialize(cache_path.read_text(encoding="utf-8", errors="ignore"))

    app = msal.PublicClientApplication(
        client_id=client_id,
        authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache,
    )
    accounts = app.get_accounts()
    if not accounts:
        return None, None, "No cached Graph account/token. Run auth_device_code.py first."

    result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result or "access_token" not in result:
        return None, None, "No valid access token (expired?). Run auth_device_code.py again."

    headers = {"Authorization": f"Bearer {result['access_token']}"}
    url = (
        f"https://graph.microsoft.com/v1.0/me/drive/root:/{inbox}:/children"
        "?$select=id,name,createdDateTime,lastModifiedDateTime,file"
    )

    try:
        res = requests.get(url, headers=headers, timeout=30)
        res.raise_for_status()
    except Exception as e:
        return None, None, f"Graph query failed: {e}"

    items = res.json().get("value", [])
    pdfs = [it for it in items if it.get("file") and str(it.get("name", "")).lower().endswith(".pdf")]

    # sort newest first by createdDateTime
    def key(it: dict[str, Any]) -> str:
        return str(it.get("createdDateTime") or "")

    pdfs_sorted = sorted(pdfs, key=key, reverse=True)
    return len(pdfs), pdfs_sorted[:limit], None


app = FastAPI(title="SortmyPDFs Dashboard")

# runtime status for buttons
LAST_ACTION: dict[str, Any] = {"ts": None, "action": None, "detail": None, "ok": None}


def _set_last(action: str, ok: bool, detail: str | None = None):
    LAST_ACTION.update({"ts": _utc_now(), "action": action, "ok": ok, "detail": detail})


def _run_cmd(cmd: list[str], cwd: Path = BASE, timeout: int = 30) -> tuple[bool, str]:
    import subprocess

    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return False, str(e)

    out = (p.stdout or "") + (p.stderr or "")
    ok = p.returncode == 0
    return ok, out.strip()[-3000:]


def _start_runner_async() -> tuple[bool, str]:
    """Kick off run_hourly.sh in the background.

    We append to a dedicated dashboard-triggered log.
    """

    import subprocess

    log_path = LOG_DIR / "dashboard-triggered.log"
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n=== dashboard trigger { _utc_now().isoformat() } ===\n")
            subprocess.Popen(["bash", str(BASE / "run_hourly.sh")], cwd=str(BASE), stdout=f, stderr=subprocess.STDOUT)
        return True, f"Started. Output appended to {log_path.relative_to(BASE)}"
    except Exception as e:
        return False, str(e)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    _require_auth(request)

    state = _load_json(STATE_PATH)
    processed = state.get("processed", {}) if isinstance(state, dict) else {}

    processed_count = len(processed) if isinstance(processed, dict) else 0
    sonstige_count = _count_sonstige(processed) if isinstance(processed, dict) else 0
    firms = _top_firms(processed, limit=10) if isinstance(processed, dict) else []

    state_imap = _load_json(STATE_IMAP_PATH)
    imap_hashes = state_imap.get("hashes", {}) if isinstance(state_imap, dict) else {}
    imap_uids = state_imap.get("uids", {}) if isinstance(state_imap, dict) else {}

    recent_logs = _list_recent_logs(limit=12)

    inbox_pdf_count, inbox_items, inbox_warn = _onedrive_inbox_live(limit=10)

    ctx = {
        "request": request,
        "now": _utc_now(),
        "processed_count": processed_count,
        "sonstige_count": sonstige_count,
        "top_firms": firms,
        "imap_uid_count": len(imap_uids) if isinstance(imap_uids, dict) else None,
        "imap_hash_count": len(imap_hashes) if isinstance(imap_hashes, dict) else None,
        "recent_logs": recent_logs,
        "inbox_pdf_count": inbox_pdf_count,
        "inbox_items": inbox_items,
        "inbox_warn": inbox_warn,
        "live_inbox_enabled": ENABLE_LIVE_INBOX,
        "buttons_enabled": ENABLE_BUTTONS,
        "last_action": LAST_ACTION,
    }

    return TEMPLATES.TemplateResponse("index.html", ctx)


@app.post("/action/run")
def action_run(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    ok, detail = _start_runner_async()
    _set_last("run_hourly.sh", ok, detail)
    return RedirectResponse(url="/", status_code=303)


@app.post("/action/timer/{mode}")
def action_timer(mode: str, request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    if mode not in {"enable", "disable"}:
        raise HTTPException(status_code=400, detail="mode must be enable|disable")

    cmd = ["systemctl", "--user", mode, "--now", "sortmypdfs.timer"]
    ok, out = _run_cmd(cmd, cwd=BASE, timeout=30)
    _set_last(f"systemctl --user {mode} --now sortmypdfs.timer", ok, out)
    return RedirectResponse(url="/", status_code=303)
