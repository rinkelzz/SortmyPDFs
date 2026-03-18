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
from starlette.datastructures import FormData

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
STATE_WEB_PATH = BASE / "state_web.json"
PORTALS_PATH = BASE / "web_portals.json"
ALIASES_PATH = BASE / "firma_aliases.json"
LOG_DIR = BASE / "logs"

# Feature flag: enable OneDrive live query for inbox count/list
ENABLE_LIVE_INBOX = os.getenv("SORTMYPDFS_DASH_LIVE_INBOX", "0") == "1"

# Auth (recommended when binding to LAN)
DASH_USER = os.getenv("SORTMYPDFS_DASH_USER")
DASH_PASS = os.getenv("SORTMYPDFS_DASH_PASS")

# Buttons / control endpoints
ENABLE_BUTTONS = os.getenv("SORTMYPDFS_DASH_BUTTONS", "0") == "1"

# Safety: cap how many items a merge operation may move per click
MERGE_MAX_ITEMS = int(os.getenv("SORTMYPDFS_DASH_MERGE_MAX_ITEMS", "200"))


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


def _load_aliases() -> dict[str, list[str]]:
    data = _load_json(ALIASES_PATH)
    out: dict[str, list[str]] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if not isinstance(k, str):
                continue
            if isinstance(v, list):
                out[k] = [str(x) for x in v if str(x).strip()]
    return out


def _save_aliases(d: dict[str, list[str]]) -> None:
    # Normalize: keep keys and values sorted for stable diffs + nicer UX.
    normalized: dict[str, list[str]] = {}
    for canon in sorted(d.keys(), key=lambda s: s.casefold()):
        als = d.get(canon) or []
        # de-dup (casefold) + sort alpha
        seen: set[str] = set()
        out: list[str] = []
        for a in als:
            s = str(a).strip()
            if not s:
                continue
            cf = s.casefold()
            if cf in seen:
                continue
            seen.add(cf)
            out.append(s)
        normalized[canon] = sorted(out, key=lambda s: s.casefold())

    ALIASES_PATH.write_text(json.dumps(normalized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _count_sonstige(processed: dict[str, Any]) -> int:
    n = 0
    for _k, v in processed.items():
        dest = str(v.get("dest", ""))
        # Expected dest like: SortmyPDFs/Sonstige/<Firma>
        if dest.startswith("SortmyPDFs/Sonstige"):
            n += 1
    return n


def _top_firms(processed: dict[str, Any], limit: int = 10) -> list[tuple[str, int]]:
    counts: dict[str, Any] = {}
    for _k, v in processed.items():
        dest = str(v.get("dest", ""))
        parts = dest.split("/")
        firm = parts[2] if len(parts) >= 3 else "(unknown)"
        counts[firm] = counts.get(firm, 0) + 1
    return sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]


def _build_state_tree(processed: dict[str, Any]) -> dict[str, Any]:
    """Build a nested tree from state.json entries.

    Structure:
      {"name": "root", "children": {folder: node}, "files": [fileEntry...]}

    fileEntry includes item_id and meta fields.
    """

    root: dict[str, Any] = {"name": "root", "children": {}, "files": []}

    for item_id, meta in processed.items():
        dest = str(meta.get("dest", ""))
        # normalize split
        parts = [p for p in dest.split("/") if p]
        node = root
        for p in parts:
            node = node["children"].setdefault(p, {"name": p, "children": {}, "files": []})
        node["files"].append(
            {
                "item_id": item_id,
                "src": meta.get("src"),
                "new": meta.get("new"),
                "dest": meta.get("dest"),
                "ts": meta.get("ts"),
            }
        )

    def sort_node(n: dict[str, Any]) -> None:
        n["files"].sort(key=lambda f: str(f.get("new") or ""))
        # sort children by name
        children_items = sorted(n["children"].items(), key=lambda kv: kv[0].lower())
        n["children"] = {k: v for k, v in children_items}
        for ch in n["children"].values():
            sort_node(ch)

    sort_node(root)
    return root


LOG_TS_RE = re.compile(r"hourly-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z)\.log$")


def _parse_log_ts(name: str) -> datetime | None:
    m = LOG_TS_RE.search(name)
    if not m:
        return None
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


def _load_web_stats() -> dict:
    """Load stats from state_web.json grouped by portal."""
    state = _load_json(STATE_WEB_PATH)
    hashes = state.get("processed_hashes", {}) if isinstance(state, dict) else {}
    by_portal: dict[str, list[dict]] = {}
    for h, meta in hashes.items():
        portal = str(meta.get("portal") or "unknown")
        by_portal.setdefault(portal, []).append({
            "hash": h,
            "filename": meta.get("filename", ""),
            "ts": meta.get("ts", ""),
        })
    # Sort each portal's docs by ts descending
    for docs in by_portal.values():
        docs.sort(key=lambda d: str(d.get("ts") or ""), reverse=True)
    return {
        "total": len(hashes),
        "by_portal": by_portal,
    }


def _load_web_portals() -> list[dict]:
    if not PORTALS_PATH.exists():
        return []
    try:
        return json.loads(PORTALS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def _start_web_ingest_async(portal: str | None = None) -> tuple[bool, str]:
    """Run web_ingest.py in the background, optionally for a single portal."""
    import subprocess

    log_path = LOG_DIR / "web-ingest.log"
    py = BASE / ".venv" / "bin" / "python"
    cmd = [str(py), "web_ingest.py"]
    if portal:
        cmd += ["--portal", portal]
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n=== dashboard trigger { _utc_now().isoformat() } portal={portal or 'all'} ===\n")
            subprocess.Popen(cmd, cwd=str(BASE), stdout=f, stderr=subprocess.STDOUT)
        label = f"Portal: {portal}" if portal else "Alle Portale"
        return True, f"{label} gestartet. Log: {log_path.relative_to(BASE)}"
    except Exception as e:
        return False, str(e)


def _graph_headers() -> tuple[dict[str, str] | None, str | None]:
    """Return (headers, warning)."""

    if msal is None or requests is None or load_dotenv is None:
        return None, "Graph requires msal/requests/python-dotenv in the venv."

    load_dotenv(BASE / ".env")
    tenant = os.getenv("GRAPH_TENANT", "consumers")
    client_id = os.getenv("GRAPH_CLIENT_ID")
    scopes = os.getenv("GRAPH_SCOPES", "https://graph.microsoft.com/Files.ReadWrite.All offline_access").split()

    if not client_id:
        return None, "GRAPH_CLIENT_ID missing in .env."

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
        return None, "No cached Graph account/token. Run auth_device_code.py first."

    result = app.acquire_token_silent(scopes, account=accounts[0])
    if not result or "access_token" not in result:
        return None, "No valid access token (expired?). Run auth_device_code.py again."

    headers = {"Authorization": f"Bearer {result['access_token']}"}
    return headers, None


def _list_children_by_id(headers: dict[str, str], item_id: str, select: str = "id,name,folder,file") -> list[dict[str, Any]]:
    url = f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}/children?$select={select}"
    out: list[dict[str, Any]] = []
    while True:
        res = requests.get(url, headers=headers, timeout=60)
        res.raise_for_status()
        data = res.json()
        out.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if not url:
            break
    return out


def _list_children_by_path(headers: dict[str, str], path: str, select: str = "id,name,folder,file") -> list[dict[str, Any]]:
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{path}:/children?$select={select}"
    out: list[dict[str, Any]] = []
    while True:
        res = requests.get(url, headers=headers, timeout=60)
        res.raise_for_status()
        data = res.json()
        out.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        if not url:
            break
    return out


try:
    from sort_and_move import firma_key as _folder_key  # noqa: E402
except ImportError:
    # Fallback if sort_and_move is not importable (e.g. missing .env)
    def _folder_key(name: str) -> str:
        s = name.casefold()
        s = re.sub(r"[\._,;:()\[\]{}+|]", " ", s)
        s = s.replace("-", " ")
        s = re.sub(r"\s+", " ", s).strip()
        drop = {
            "gmbh", "ag", "eg", "kg", "se", "mbh", "gbr",
            "ev", "e.v", "a.g", "versicherung",
        }
        words = [w for w in s.split(" ") if w and w not in drop]
        return " ".join(words)


def _onedrive_inbox_live(limit: int = 10) -> tuple[int | None, list[dict[str, Any]] | None, str | None]:
    """Returns (pdf_count, items, warning)."""

    if not ENABLE_LIVE_INBOX:
        return None, None, "Live inbox disabled (set SORTMYPDFS_DASH_LIVE_INBOX=1 to enable)."

    headers, warn = _graph_headers()
    if warn:
        return None, None, warn

    inbox = os.getenv("ONEDRIVE_INBOX", "vomDrucker")
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

# runtime cache for empty-folder scan results
EMPTY_FOLDERS: list[dict[str, Any]] = []
EMPTY_SCAN_RUNNING: bool = False

# runtime cache for merge proposals
MERGE_PROPOSALS: list[dict[str, Any]] = []


_GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9!_\-.:]{4,512}$")


def _valid_graph_id(value: str) -> bool:
    """Return True only if *value* looks like a valid OneDrive/Graph item ID."""
    return bool(value and _GRAPH_ID_RE.match(value))


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


def _nav_stats() -> dict[str, Any]:
    """Small stats for the navbar (best-effort, should not break page render)."""

    stats: dict[str, Any] = {"nav_inbox": None, "nav_errors": None}

    # Inbox (only if live mode enabled)
    try:
        c, _items, _warn = _onedrive_inbox_live(limit=0)
        stats["nav_inbox"] = c
    except Exception:
        pass

    # Errors in recent logs
    try:
        recent = _list_recent_logs(limit=6)
        err_runs = sum(1 for lg in recent if (lg.level_counts.get("ERROR", 0) or 0) > 0)
        stats["nav_errors"] = err_runs
    except Exception:
        pass

    return stats


def _base_ctx(request: Request) -> dict[str, Any]:
    return {
        "request": request,
        "now": _utc_now(),
        "buttons_enabled": ENABLE_BUTTONS,
        "last_action": LAST_ACTION,
        **_nav_stats(),
    }


def _load_state_metrics() -> dict[str, Any]:
    state = _load_json(STATE_PATH)
    processed = state.get("processed", {}) if isinstance(state, dict) else {}

    processed_count = len(processed) if isinstance(processed, dict) else 0
    sonstige_count = _count_sonstige(processed) if isinstance(processed, dict) else 0
    firms = _top_firms(processed, limit=10) if isinstance(processed, dict) else []

    tree = _build_state_tree(processed) if isinstance(processed, dict) else {"name": "root", "children": {}, "files": []}

    return {
        "state": state,
        "processed": processed,
        "processed_count": processed_count,
        "sonstige_count": sonstige_count,
        "top_firms": firms,
        "state_tree": tree,
    }


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    _require_auth(request)

    m = _load_state_metrics()

    state_imap = _load_json(STATE_IMAP_PATH)
    imap_hashes = state_imap.get("hashes", {}) if isinstance(state_imap, dict) else {}
    imap_uids = state_imap.get("uids", {}) if isinstance(state_imap, dict) else {}

    recent_logs = _list_recent_logs(limit=12)
    inbox_pdf_count, inbox_items, inbox_warn = _onedrive_inbox_live(limit=10)

    web_stats = _load_web_stats()
    ctx = {
        **_base_ctx(request),
        "title": "SortmyPDFs – Übersicht",
        "active": "overview",
        "processed_count": m["processed_count"],
        "sonstige_count": m["sonstige_count"],
        "top_firms": m["top_firms"],
        "imap_uid_count": len(imap_uids) if isinstance(imap_uids, dict) else None,
        "imap_hash_count": len(imap_hashes) if isinstance(imap_hashes, dict) else None,
        "recent_logs": recent_logs,
        "inbox_pdf_count": inbox_pdf_count,
        "inbox_items": inbox_items,
        "inbox_warn": inbox_warn,
        "live_inbox_enabled": ENABLE_LIVE_INBOX,
        "web_total": web_stats["total"],
    }

    return TEMPLATES.TemplateResponse("overview.html", ctx)


@app.get("/state", response_class=HTMLResponse)
def state_browser(request: Request):
    _require_auth(request)
    m = _load_state_metrics()
    ctx = {
        **_base_ctx(request),
        "title": "SortmyPDFs – State Browser",
        "active": "state",
        "state_tree": m["state_tree"],
    }
    return TEMPLATES.TemplateResponse("state.html", ctx)


@app.get("/merge", response_class=HTMLResponse)
def merge_page(request: Request):
    _require_auth(request)
    ctx = {
        **_base_ctx(request),
        "title": "SortmyPDFs – Merges",
        "active": "merge",
        "merge_proposals": MERGE_PROPOSALS,
    }
    return TEMPLATES.TemplateResponse("merge.html", ctx)


@app.get("/empty-folders", response_class=HTMLResponse)
def empty_page(request: Request):
    _require_auth(request)
    ctx = {
        **_base_ctx(request),
        "title": "SortmyPDFs – Leere Ordner",
        "active": "empty",
        "empty_folders": EMPTY_FOLDERS,
        "empty_scan_running": EMPTY_SCAN_RUNNING,
    }
    return TEMPLATES.TemplateResponse("empty_folders.html", ctx)


@app.get("/aliases", response_class=HTMLResponse)
def aliases_page(request: Request):
    _require_auth(request)
    ctx = {
        **_base_ctx(request),
        "title": "SortmyPDFs – Aliase",
        "active": "aliases",
        "aliases": _load_aliases(),
    }
    return TEMPLATES.TemplateResponse("aliases.html", ctx)


@app.get("/web-portals", response_class=HTMLResponse)
def web_portals_page(request: Request):
    _require_auth(request)
    portals = _load_web_portals()
    web_stats = _load_web_stats()
    ctx = {
        **_base_ctx(request),
        "title": "SortmyPDFs – Web Portale",
        "active": "web",
        "portals": portals,
        "web_stats": web_stats,
    }
    return TEMPLATES.TemplateResponse("web_portals.html", ctx)


@app.post("/action/run")
def action_run(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    ok, detail = _start_runner_async()
    _set_last("run_hourly.sh", ok, detail)
    return RedirectResponse(url="/", status_code=303)


@app.post("/action/run-web")
async def action_run_web(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    form: FormData = await request.form()
    portal = (form.get("portal") or "").strip() or None
    ok, detail = _start_web_ingest_async(portal)
    _set_last(f"web_ingest {'–' + portal if portal else '(alle)'}", ok, detail)
    return RedirectResponse(url="/web-portals", status_code=303)


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


@app.post("/action/reprocess")
async def action_reprocess(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    form: FormData = await request.form()
    item_id = (form.get("item_id") or "").strip()
    if not item_id:
        _set_last("reprocess", False, "Missing item_id")
        return RedirectResponse(url="/", status_code=303)
    if not _valid_graph_id(item_id):
        _set_last("reprocess", False, "Invalid item_id format")
        return RedirectResponse(url="/", status_code=303)

    py = BASE / ".venv" / "bin" / "python"
    cmd = [str(py), "sort_and_move.py", "--apply", "--reprocess", item_id]
    ok, out = _run_cmd(cmd, cwd=BASE, timeout=180)
    _set_last(f"reprocess {item_id}", ok, out)
    return RedirectResponse(url="/", status_code=303)


def _scan_empty_folders_under_root(root_path: str = "SortmyPDFs") -> tuple[bool, str]:
    """Scan OneDrive for empty folders under root_path.

    Requires Graph auth.
    Stores results in EMPTY_FOLDERS.
    """

    if not ENABLE_LIVE_INBOX:
        return False, "Empty-folder scan requires Graph mode (set SORTMYPDFS_DASH_LIVE_INBOX=1)."

    headers, warn = _graph_headers()
    if warn:
        return False, warn

    # Find root folder children
    start_url = (
        f"https://graph.microsoft.com/v1.0/me/drive/root:/{root_path}:/children"
        "?$select=id,name,folder,parentReference"
    )

    empty: list[dict[str, Any]] = []

    def list_children(folder_id: str) -> list[dict[str, Any]]:
        url = (
            f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}/children"
            "?$select=id,name,folder,parentReference"
        )
        out: list[dict[str, Any]] = []
        while True:
            res = requests.get(url, headers=headers, timeout=60)
            res.raise_for_status()
            data = res.json()
            out.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            if not url:
                break
        return out

    def walk(node_id: str, path: str) -> None:
        children = list_children(node_id)
        folders = [c for c in children if c.get("folder")]
        if not children:
            empty.append({"id": node_id, "path": path})
            return
        for f in folders:
            walk(f["id"], f"{path}/{f.get('name')}")

        # after walking children, re-check: if only empty children existed, parent may now be effectively empty
        # BUT we do not delete parents automatically in scan; user will select.

    try:
        res = requests.get(start_url, headers=headers, timeout=60)
        res.raise_for_status()
        top = res.json().get("value", [])
        top_folders = [c for c in top if c.get("folder")]
        for f in top_folders:
            walk(f["id"], f"{root_path}/{f.get('name')}")
    except Exception as e:
        return False, f"Graph scan failed: {e}"

    # sort deepest first
    empty.sort(key=lambda x: len(str(x.get("path", "")).split("/")), reverse=True)

    EMPTY_FOLDERS.clear()
    EMPTY_FOLDERS.extend(empty)

    return True, f"Found {len(empty)} empty folders under /{root_path}."


def _start_empty_scan_async(root: str = "SortmyPDFs") -> tuple[bool, str]:
    global EMPTY_SCAN_RUNNING

    if EMPTY_SCAN_RUNNING:
        return False, "Empty-folder scan already running."

    EMPTY_SCAN_RUNNING = True
    _set_last("empty-folders scan", True, "Started scan in background…")

    import threading

    def run():
        global EMPTY_SCAN_RUNNING
        try:
            ok, detail = _scan_empty_folders_under_root(root)
            _set_last("empty-folders scan", ok, detail)
        finally:
            EMPTY_SCAN_RUNNING = False

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return True, "Started"


@app.post("/action/empty-folders/scan")
def action_empty_scan(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    ok, detail = _start_empty_scan_async("SortmyPDFs")
    if not ok:
        _set_last("empty-folders scan", False, detail)
    return RedirectResponse(url="/empty-folders", status_code=303)


@app.post("/action/empty-folders/delete")
async def action_empty_delete(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    if not ENABLE_LIVE_INBOX:
        _set_last("empty-folders delete", False, "Requires Graph mode (SORTMYPDFS_DASH_LIVE_INBOX=1).")
        return RedirectResponse(url="/", status_code=303)

    headers, warn = _graph_headers()
    if warn:
        _set_last("empty-folders delete", False, warn)
        return RedirectResponse(url="/", status_code=303)

    form: FormData = await request.form()
    ids = form.getlist("folder_id") if hasattr(form, "getlist") else []

    if not ids:
        _set_last("empty-folders delete", False, "No folders selected")
        return RedirectResponse(url="/", status_code=303)

    invalid = [i for i in ids if not _valid_graph_id(str(i))]
    if invalid:
        _set_last("empty-folders delete", False, f"Invalid folder_id format: {invalid[:3]}")
        return RedirectResponse(url="/", status_code=303)

    # delete deepest first based on cached paths
    path_by_id = {f.get("id"): f.get("path") for f in EMPTY_FOLDERS}
    ids_sorted = sorted(ids, key=lambda i: len(str(path_by_id.get(i, "")).split("/")), reverse=True)

    deleted = 0
    errs: list[str] = []
    for folder_id in ids_sorted:
        url = f"https://graph.microsoft.com/v1.0/me/drive/items/{folder_id}"
        try:
            res = requests.delete(url, headers=headers, timeout=60)
            if res.status_code in (204, 200):
                deleted += 1
            else:
                errs.append(f"{folder_id}: HTTP {res.status_code} {res.text[:200]}")
        except Exception as e:
            errs.append(f"{folder_id}: {e}")

    # remove deleted from cache
    remaining = [f for f in EMPTY_FOLDERS if f.get("id") not in set(ids)]
    EMPTY_FOLDERS.clear()
    EMPTY_FOLDERS.extend(remaining)

    ok = len(errs) == 0
    detail = f"Deleted {deleted}/{len(ids)} empty folders." + ("\n" + "\n".join(errs[:10]) if errs else "")
    _set_last("empty-folders delete", ok, detail)
    return RedirectResponse(url="/", status_code=303)


def _scan_merge_proposals() -> tuple[bool, str]:
    """Find duplicate-ish company folders per recipient and propose merges.

    Canonical target folder = shortest name in the group.
    """

    if not ENABLE_LIVE_INBOX:
        return False, "Merge scan requires Graph mode (SORTMYPDFS_DASH_LIVE_INBOX=1)."

    headers, warn = _graph_headers()
    if warn:
        return False, warn

    try:
        roots = _list_children_by_path(headers, "SortmyPDFs", select="id,name,folder")
    except Exception as e:
        return False, f"Graph query failed: {e}"

    recipients = [r for r in roots if r.get("folder") and r.get("name") in {"Tim", "Chantal", "Sonstige"}]
    proposals: list[dict[str, Any]] = []

    for r in recipients:
        rid = r["id"]
        rname = r.get("name")
        children = _list_children_by_id(headers, rid, select="id,name,folder")
        folders = [c for c in children if c.get("folder")]

        # Build fuzzy-ish groups based on normalized keys.
        # We group if keys are equal OR one is a prefix of the other (common: "... bund" suffix).
        items2: list[dict[str, Any]] = []
        for f in folders:
            name = str(f.get("name") or "")
            if not name:
                continue
            items2.append({"id": f.get("id"), "name": name, "key": _folder_key(name)})

        # Union-Find
        parent = list(range(len(items2)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        def similar(k1: str, k2: str) -> bool:
            if not k1 or not k2:
                return False
            if k1 == k2:
                return True
            # prefix containment (at least 2 tokens to avoid over-grouping)
            t1 = k1.split(" ")
            t2 = k2.split(" ")
            if len(t1) < 2 or len(t2) < 2:
                return False
            if k1.startswith(k2) or k2.startswith(k1):
                # ensure the first 2 tokens match
                return t1[:2] == t2[:2]
            return False

        for i in range(len(items2)):
            for j in range(i + 1, len(items2)):
                if similar(items2[i]["key"], items2[j]["key"]):
                    union(i, j)

        groups: dict[int, list[dict[str, Any]]] = {}
        for idx, it in enumerate(items2):
            groups.setdefault(find(idx), []).append(it)

        for g in groups.values():
            if len(g) < 2:
                continue

            # Filter out already-empty folders (after a merge, sources may be empty but still exist)
            with_counts: list[dict[str, Any]] = []
            for it in g:
                try:
                    n = len(_list_children_by_id(headers, it["id"], select="id"))
                except Exception:
                    n = 0
                with_counts.append({**it, "child_count": n})

            nonempty = [it for it in with_counts if int(it.get("child_count") or 0) > 0]
            if len(nonempty) < 2:
                continue

            # choose canonical shortest folder name among non-empty
            g_sorted = sorted(nonempty, key=lambda x: (len(x.get("name") or ""), (x.get("name") or "").casefold()))
            target = {"id": g_sorted[0]["id"], "name": g_sorted[0]["name"]}
            sources = [{"id": it["id"], "name": it["name"]} for it in g_sorted[1:]]
            proposals.append({
                "recipient": rname,
                "key": g_sorted[0]["key"],
                "target": target,
                "sources": sources,
            })

    MERGE_PROPOSALS.clear()
    MERGE_PROPOSALS.extend(sorted(proposals, key=lambda p: (p.get("recipient") or "", p.get("key") or "")))

    return True, f"Found {len(MERGE_PROPOSALS)} merge groups."


@app.post("/action/merge/scan")
def action_merge_scan(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    ok, detail = _scan_merge_proposals()
    _set_last("merge scan", ok, detail)
    return RedirectResponse(url="/", status_code=303)


@app.post("/action/aliases/add")
async def action_alias_add(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    form: FormData = await request.form()
    canonical = (form.get("canonical") or "").strip()
    aliases_raw = (form.get("aliases") or "").strip()

    if not canonical:
        _set_last("alias add", False, "Missing canonical name")
        return RedirectResponse(url="/", status_code=303)

    aliases = [a.strip() for a in aliases_raw.splitlines() if a.strip()]
    if not aliases:
        _set_last("alias add", False, "No aliases provided")
        return RedirectResponse(url="/", status_code=303)

    d = _load_aliases()
    d.setdefault(canonical, [])
    # de-dup (casefold)
    existing_cf = {x.casefold() for x in d[canonical]}
    for a in aliases:
        if a.casefold() not in existing_cf:
            d[canonical].append(a)
            existing_cf.add(a.casefold())

    # sort alpha (case-insensitive)
    d[canonical] = sorted(d[canonical], key=lambda s: s.casefold())
    _save_aliases(d)
    _set_last("alias add", True, f"Saved {len(aliases)} aliases under '{canonical}'.")
    return RedirectResponse(url="/", status_code=303)


@app.post("/action/aliases/delete")
async def action_alias_delete(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    form: FormData = await request.form()
    canonical = (form.get("canonical") or "").strip()
    alias = (form.get("alias") or "").strip()

    d = _load_aliases()
    if canonical not in d:
        _set_last("alias delete", False, "Canonical not found")
        return RedirectResponse(url="/", status_code=303)

    if alias:
        before = len(d[canonical])
        d[canonical] = [a for a in d[canonical] if a != alias]
        after = len(d[canonical])
        if after == 0:
            d.pop(canonical, None)
        _save_aliases(d)
        _set_last("alias delete", True, f"Removed {before-after} alias(es).")
        return RedirectResponse(url="/", status_code=303)

    # if alias is empty, delete whole canonical block
    d.pop(canonical, None)
    _save_aliases(d)
    _set_last("alias delete", True, f"Removed '{canonical}'")
    return RedirectResponse(url="/", status_code=303)


@app.post("/action/merge/apply")
async def action_merge_apply(request: Request):
    _require_auth(request)
    if not ENABLE_BUTTONS:
        raise HTTPException(status_code=404)

    if not ENABLE_LIVE_INBOX:
        _set_last("merge apply", False, "Requires Graph mode (SORTMYPDFS_DASH_LIVE_INBOX=1).")
        return RedirectResponse(url="/", status_code=303)

    headers, warn = _graph_headers()
    if warn:
        _set_last("merge apply", False, warn)
        return RedirectResponse(url="/", status_code=303)

    form: FormData = await request.form()
    moves = form.getlist("move") if hasattr(form, "getlist") else []
    # move entries are "sourceId->targetId"
    if not moves:
        _set_last("merge apply", False, "No folders selected")
        return RedirectResponse(url="/", status_code=303)

    # Validate all IDs before any network calls
    for spec in moves:
        if "->" not in spec:
            _set_last("merge apply", False, f"Invalid move spec (missing '->'): {spec[:80]}")
            return RedirectResponse(url="/", status_code=303)
        sid, tid = spec.split("->", 1)
        if not _valid_graph_id(sid) or not _valid_graph_id(tid):
            _set_last("merge apply", False, f"Invalid Graph ID in move spec: {spec[:80]}")
            return RedirectResponse(url="/", status_code=303)

    # Safety preflight: count how many children would be moved
    counts: list[tuple[str, int]] = []
    total = 0
    parsed: list[tuple[str, str]] = []
    for spec in moves:
        if "->" not in spec:
            continue
        source_id, target_id = spec.split("->", 1)
        parsed.append((source_id, target_id))
        try:
            children = _list_children_by_id(headers, source_id, select="id")
            n = len(children)
        except Exception:
            n = 0
        counts.append((source_id, n))
        total += n

    if total > MERGE_MAX_ITEMS:
        detail = (
            f"Refusing to move {total} items (limit={MERGE_MAX_ITEMS}).\n"
            "Select fewer folders, or raise SORTMYPDFS_DASH_MERGE_MAX_ITEMS in ~/.config/sortmypdfs-dashboard.env and restart the service.\n"
            "Counts per selected source folder (id: count):\n"
            + "\n".join([f"- {sid}: {n}" for sid, n in counts[:20]])
        )
        _set_last("merge apply", False, detail)
        return RedirectResponse(url="/", status_code=303)

    moved_items = 0
    errs: list[str] = []

    for source_id, target_id in parsed:
        try:
            children = _list_children_by_id(headers, source_id, select="id,name,folder,file")
        except Exception as e:
            errs.append(f"list {source_id}: {e}")
            continue

        for ch in children:
            cid = ch.get("id")
            if not cid:
                continue
            url = f"https://graph.microsoft.com/v1.0/me/drive/items/{cid}"
            payload = {"parentReference": {"id": target_id}}
            try:
                res = requests.patch(
                    url,
                    headers={**headers, "Content-Type": "application/json"},
                    json=payload,
                    timeout=60,
                )
                if res.status_code in (200, 201):
                    moved_items += 1
                else:
                    errs.append(f"move {cid}: HTTP {res.status_code} {res.text[:160]}")
            except Exception as e:
                errs.append(f"move {cid}: {e}")

    ok = len(errs) == 0
    detail = f"Moved {moved_items} items." + ("\n" + "\n".join(errs[:10]) if errs else "")
    _set_last("merge apply", ok, detail)
    return RedirectResponse(url="/", status_code=303)
