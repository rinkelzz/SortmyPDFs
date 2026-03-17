from __future__ import annotations

import logging
import os
import re
import json
import time
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import msal
import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
_LOG_LEVEL = os.getenv("SORTMYPDFS_LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("sort_and_move")


# ---------------------------------------------------------------------------
# Configuration validation
# ---------------------------------------------------------------------------
def _validate_config() -> None:
    """Fail fast with a clear message if required env vars are missing."""
    missing = [k for k in ("GRAPH_CLIENT_ID",) if not os.getenv(k)]
    if missing:
        raise SystemExit(
            f"Missing required environment variable(s): {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )


_validate_config()

TENANT = os.getenv("GRAPH_TENANT", "consumers")
CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
SCOPES = os.getenv("GRAPH_SCOPES", "Files.ReadWrite.All").split()
INBOX = os.getenv("ONEDRIVE_INBOX", "vomDrucker")
TARGET_ROOT = os.getenv("ONEDRIVE_TARGET_ROOT", "SortmyPDFs")

STATE_PATH = BASE / "state.json"
TOKEN_CACHE_PATH = BASE / ".token_cache.bin"
TMP_DIR = BASE / ".tmp"
ALIASES_PATH = BASE / "firma_aliases.json"

SESSION = requests.Session()

# cache: recipient_path -> {firma_key(name): {"name": name, "id": id}}
FOLDER_CACHE: dict[str, dict[str, dict]] = {}


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"processed": {}}


def save_state(state: dict) -> None:
    """Write state atomically to avoid corruption if interrupted mid-write."""
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_PATH)


def get_app() -> tuple[msal.PublicClientApplication, msal.SerializableTokenCache]:
    if not CLIENT_ID:
        raise SystemExit("Missing GRAPH_CLIENT_ID in .env")
    cache = msal.SerializableTokenCache()
    if TOKEN_CACHE_PATH.exists():
        cache.deserialize(TOKEN_CACHE_PATH.read_text())
    app = msal.PublicClientApplication(
        client_id=CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{TENANT}",
        token_cache=cache,
    )
    return app, cache


def get_token() -> str:
    app, cache = get_app()
    accounts = app.get_accounts()
    if not accounts:
        raise SystemExit("No cached account. Run auth_device_code.py first.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0])
    if not result or "access_token" not in result:
        raise SystemExit(f"No token in cache (or expired). Run auth_device_code.py. Got: {result}")
    if cache.has_state_changed:
        TOKEN_CACHE_PATH.write_text(cache.serialize())
    return result["access_token"]


def graph(method: str, url: str, token: str, _retries: int = 3, _backoff: float = 2.0, **kwargs) -> requests.Response:
    """Make a Graph API request with exponential-backoff retry on transient errors (429/503/5xx)."""
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Accept", "application/json")
    last_exc: Exception | None = None
    for attempt in range(_retries + 1):
        try:
            resp = SESSION.request(method, url, headers=headers, timeout=60, **kwargs)
            if resp.status_code in (429, 503) and attempt < _retries:
                retry_after = float(resp.headers.get("Retry-After", _backoff * (2 ** attempt)))
                log.warning("Graph API rate-limited (%d). Retrying in %.0fs…", resp.status_code, retry_after)
                time.sleep(retry_after)
                continue
            return resp
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_exc = exc
            if attempt < _retries:
                wait = _backoff * (2 ** attempt)
                log.warning("Graph API connection error (attempt %d/%d). Retrying in %.0fs…", attempt + 1, _retries, wait)
                time.sleep(wait)
    if last_exc:
        raise last_exc
    raise RuntimeError("graph() exhausted retries without a response")


def list_inbox_pdfs(token: str) -> list[dict]:
    """List all PDF files in the inbox folder, following @odata.nextLink for pagination."""
    url: str | None = (
        f"https://graph.microsoft.com/v1.0/me/drive/root:/{INBOX}:/children"
        "?$select=id,name,createdDateTime,lastModifiedDateTime,file"
    )
    all_items: list[dict] = []
    while url:
        res = graph("GET", url, token)
        res.raise_for_status()
        data = res.json()
        all_items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return [it for it in all_items if it.get("file") and it["name"].lower().endswith(".pdf")]


def download_item(token: str, item_id: str, out_path: Path) -> None:
    """Download file contents via Graph.

    Note: For some account types/permissions, `@microsoft.graph.downloadUrl` may not be returned.
    The `/content` endpoint is the most reliable: it returns a 302 to the actual download URL.
    """
    url = f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}/content"
    headers = {"Authorization": f"Bearer {token}"}
    r = SESSION.get(url, headers=headers, stream=True, timeout=180, allow_redirects=True)
    r.raise_for_status()
    with out_path.open("wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def _find_bin(name: str) -> str:
    """Find an executable, even when systemd PATH is minimal.

    We prefer PATH, but fall back to common locations (incl. linuxbrew).
    """

    p = shutil.which(name)
    if p:
        return p

    candidates = [
        f"/usr/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/home/linuxbrew/.linuxbrew/bin/{name}",
        str(Path.home() / ".linuxbrew" / "bin" / name),
    ]
    for c in candidates:
        if Path(c).exists():
            return c

    raise FileNotFoundError(
        f"Required binary '{name}' not found. Install it (e.g. poppler-utils for pdftoppm, tesseract-ocr for tesseract) "
        "or fix PATH for the service."
    )


def ocr_first_page(pdf_path: Path) -> str:
    TMP_DIR.mkdir(exist_ok=True)

    pdftoppm = _find_bin("pdftoppm")
    tesseract = _find_bin("tesseract")

    stem = TMP_DIR / (pdf_path.stem + "-page")
    # render first page to png
    subprocess.run([
        pdftoppm,
        "-f",
        "1",
        "-l",
        "1",
        "-png",
        "-r",
        "200",
        str(pdf_path),
        str(stem),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # pdftoppm usually writes "-1.png" but some versions/configs use zero-padded numbering ("-01.png").
    candidates = sorted(TMP_DIR.glob(stem.name + "-*.png"))
    if not candidates:
        raise FileNotFoundError(f"pdftoppm produced no PNGs for {pdf_path}")
    img = candidates[0]

    out_base = TMP_DIR / (pdf_path.stem + "-ocr")
    try:
        subprocess.run([
            tesseract,
            str(img),
            str(out_base),
            "-l",
            "deu",
            "--psm",
            "6",
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        txt_path = Path(str(out_base) + ".txt")
        return txt_path.read_text(encoding="utf-8", errors="ignore")
    finally:
        # Clean up temp OCR artifacts
        for f in candidates:
            f.unlink(missing_ok=True)
        txt_path = Path(str(out_base) + ".txt")
        txt_path.unlink(missing_ok=True)


def pick_recipient(text: str, fallback_name: str) -> str:
    t = (text + "\n" + fallback_name).lower()
    # prefer explicit names
    if "chantal" in t:
        return "Chantal"
    if "tim" in t and "rinkel" in t:
        return "Tim"
    if re.search(r"\bherrn\b", t) and "rinkel" in t:
        return "Tim"
    # fallback heuristics from filename
    if "tim" in fallback_name.lower() or "rinkel" in fallback_name.lower():
        return "Tim"
    return "Sonstige"


def firma_from_filename(fallback_name: str) -> str | None:
    n = re.sub(r"\.pdf$", "", fallback_name, flags=re.I)
    # strip leading date like YYYY-MM-DD
    n = re.sub(r"^\d{4}-\d{2}-\d{2}\s+", "", n)
    # common patterns: "<Firma> Rechnung <nr>", "<Firma> Laborbefund", etc.
    m = re.match(r"(.+?)\s+(Rechnung|Beitragsrechnung|Laborbefund|Kaufvertrag|Bescheid)\b", n, flags=re.I)
    if m:
        return normalize_firma(m.group(1))
    # else: take first 5 words as fallback firma
    words = [w for w in re.split(r"\s+", n) if w]
    if not words:
        return None
    return normalize_firma(" ".join(words[:5]))


def normalize_firma(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    # cut off common noise tokens
    name = re.split(r"\b(Kundennummer|Kundenservice|IBAN|Telefon|Fax)\b", name, flags=re.I)[0].strip()
    # avoid crazy path chars
    name = re.sub(r"[\\/:*?\"<>|]", "-", name)
    return name[:80] if len(name) > 80 else name


def firma_key(name: str) -> str:
    """Key for de-duplicating companies / folder reuse.

    We intentionally normalize aggressively so that minor OCR variants land in the same folder.
    """

    s = name.casefold()
    s = re.sub(r"[\._,;:()\[\]{}+|]", " ", s)
    s = s.replace("-", " ")
    s = re.sub(r"\s+", " ", s).strip()

    # drop common legal forms / noise words for matching
    drop = {
        "gmbh",
        "ag",
        "eg",
        "kg",
        "se",
        "mbh",
        "gbr",
        "ev",
        "e.v",
        "a.g",
        "a.",
        "g.",
        "versicherung",
    }
    words = [w for w in s.split(" ") if w and w not in drop]
    return " ".join(words)


_ALIASES_CACHE: dict[str, list[str]] | None = None
_ALIASES_MTIME: float = 0.0


def load_aliases() -> dict[str, list[str]]:
    global _ALIASES_CACHE, _ALIASES_MTIME
    try:
        if ALIASES_PATH.exists():
            mtime = ALIASES_PATH.stat().st_mtime
            if _ALIASES_CACHE is not None and mtime == _ALIASES_MTIME:
                return _ALIASES_CACHE
            data = json.loads(ALIASES_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                out: dict[str, list[str]] = {}
                for k, v in data.items():
                    if not isinstance(k, str):
                        continue
                    if isinstance(v, list):
                        out[k] = [str(x) for x in v if str(x).strip()]
                _ALIASES_CACHE = out
                _ALIASES_MTIME = mtime
                return out
    except json.JSONDecodeError as exc:
        log.error("firma_aliases.json is malformed and could not be loaded: %s", exc)
    except OSError as exc:
        log.error("Could not read firma_aliases.json: %s", exc)
    return {}


def pick_firma(text: str, fallback_name: str) -> str:
    # Always prefer HDI if present (your rule)
    if re.search(r"\bHDI\b", text):
        return "HDI"
    if "hdi" in fallback_name.lower():
        return "HDI"

    # User-defined aliases: if any alias substring occurs, pick canonical company name.
    t_cf = (text or "").casefold()
    for canonical, aliases in load_aliases().items():
        for a in aliases:
            a_cf = (a or "").casefold().strip()
            if a_cf and a_cf in t_cf:
                return canonical

    # High-confidence special cases (kept for backwards compatibility)
    if re.search(r"deutsche\s+rentenversicherung", text, flags=re.I):
        # Often appears as "Deutsche Rentenversicherung Bund"
        if re.search(r"deutsche\s+rentenversicherung\s+bund", text, flags=re.I):
            return "Deutsche Rentenversicherung Bund"
        return "Deutsche Rentenversicherung"

    # Use filename as a strong signal (usually cleaner than OCR)
    from_fn = firma_from_filename(fallback_name)

    # OCR heuristic: first plausible org-like line in first ~20 lines, but strip addresses
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = lines[:25]
    org_re = re.compile(
        r"\b(GmbH|AG|eG|Versicherung|Krankenkasse|Bank|Finanzdienstleistungen|Kirchenamt|Rentenversicherung)\b",
        flags=re.I,
    )

    def cleanup(line: str) -> str:
        # remove obvious address tails after separators
        line = re.split(r"\s+[\-•«=]{1,}\s+", line)[0]
        line = re.sub(r"\s+", " ", line).strip()
        # drop postal codes/street fragments if they slipped in
        line = re.sub(r"\b\d{5}\b.*$", "", line).strip()
        return normalize_firma(line)

    for ln in head:
        if len(ln) < 4:
            continue
        if org_re.search(ln):
            cleaned = cleanup(ln)
            if cleaned and len(cleaned) >= 3:
                # Skip lines that are only the legal form (common OCR artifact)
                if re.fullmatch(r"(GmbH|AG|eG)", cleaned, flags=re.I):
                    continue
                # prefer non-garbage over filename
                return cleaned

    return from_fn or "Unbekannt"


def pick_doc_type(text: str, fallback_name: str) -> str:
    t = (text + "\n" + fallback_name).lower()
    for key, label in [
        ("beitragsrechnung", "Beitragsrechnung"),
        ("rechnung", "Rechnung"),
        ("kaufvertrag", "Kaufvertrag"),
        ("laborbefund", "Laborbefund"),
        ("bescheid", "Bescheid"),
    ]:
        if key in t:
            return label
    return "Dokument"


def pick_date(text: str, created_dt: str, fallback_name: str) -> str:
    # If filename starts with YYYY-MM-DD, treat as strong fallback (often correct for scans)
    mfn = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?=[^0-9]|$)", fallback_name)
    fn_date = None
    if mfn:
        try:
            fn_date = datetime(int(mfn.group(1)), int(mfn.group(2)), int(mfn.group(3)))
        except ValueError:
            fn_date = None
    # Collect all dd.mm.yyyy dates, choose the most plausible 'document date'
    dates = []
    for m in re.finditer(r"\b(\d{1,2})\.(\d{1,2})\.(\d{2,4})\b", text):
        d, mo, y = m.group(1), m.group(2), m.group(3)
        if len(y) == 2:
            y = "20" + y
        try:
            dt = datetime(int(y), int(mo), int(d))
        except ValueError:
            continue
        # filter out very old dates (often birth dates etc.)
        if dt.year < 2010 or dt.year > 2035:
            continue
        # score: prefer dates near typical header patterns (city, comma)
        window = text[max(0, m.start()-20):m.end()+20]
        score = 0
        if "," in window or "hannover" in window.lower() or "berlin" in window.lower():
            score += 2
        if "\n" in window:
            score += 1
        dates.append((score, dt))

    if dates:
        # highest score, then latest date
        dates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best = dates[0][1]
        # If we also have a filename date, and OCR date looks suspiciously far away (> 370 days), prefer filename.
        if fn_date and abs((best - fn_date).days) > 370:
            return fn_date.strftime("%Y-%m-%d")
        return best.strftime("%Y-%m-%d")

    # fallback: filename date, then createdDateTime
    if fn_date:
        return fn_date.strftime("%Y-%m-%d")

    # createdDateTime is ISO
    try:
        dt = datetime.fromisoformat(created_dt.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def ensure_folder(token, path: str):
    # Create folder by path (idempotent). Use conflictBehavior=replace? better: fail if exists? We'll do rename behavior but name is deterministic.
    url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{path}"
    res = graph("GET", url, token)
    if res.status_code == 200:
        return res.json().get("id")

    if "/" in path:
        parent, name = path.rsplit("/", 1)
        parent_url = f"https://graph.microsoft.com/v1.0/me/drive/root:/{parent}:/children"
    else:
        parent, name = "", path
        parent_url = "https://graph.microsoft.com/v1.0/me/drive/root/children"
    payload = {
        "name": name,
        "folder": {},
        "@microsoft.graph.conflictBehavior": "fail",
    }
    res2 = graph("POST", parent_url, token, json=payload)
    if res2.status_code in (200, 201):
        return res2.json().get("id")
    if res2.status_code == 409:
        # created concurrently; fetch again
        res3 = graph("GET", url, token)
        res3.raise_for_status()
        return res3.json().get("id")
    res2.raise_for_status()


def list_child_folders_by_path(token: str, path: str) -> list[dict]:
    """List direct child folders under a given OneDrive path."""

    url: str | None = (
        f"https://graph.microsoft.com/v1.0/me/drive/root:/{path}:/children"
        "?$select=id,name,folder"
    )
    all_items: list[dict] = []
    while url:
        res = graph("GET", url, token)
        res.raise_for_status()
        data = res.json()
        all_items.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
    return [it for it in all_items if it.get("folder")]


def resolve_existing_firma_folder(token: str, recipient: str, firma: str) -> str:
    """If a matching folder already exists under SortmyPDFs/<recipient>/, reuse its name.

    Returns the folder NAME to use (not the id). Matching is based on firma_key().
    """

    parent_path = f"{TARGET_ROOT}/{recipient}"

    if parent_path not in FOLDER_CACHE:
        m: dict[str, dict] = {}
        try:
            folders = list_child_folders_by_path(token, parent_path)
        except requests.exceptions.RequestException as exc:
            log.warning("Could not list folders under %s: %s", parent_path, exc)
            folders = []
        for f in folders:
            name = str(f.get("name") or "")
            if not name:
                continue
            m[firma_key(name)] = {"name": name, "id": f.get("id")}
        FOLDER_CACHE[parent_path] = m

    key = firma_key(firma)
    hit = FOLDER_CACHE[parent_path].get(key)
    if hit and hit.get("name"):
        return str(hit["name"])
    return firma


def move_and_rename(token, item_id: str, dest_folder_id: str, new_name: str):
    """Move an item to dest_folder_id and rename it.

    If the destination already has a file with the same name, OneDrive returns 409.
    In that case, retry with a numbered suffix.
    """
    url = f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}"

    base, ext = os.path.splitext(new_name)
    ext = ext or ".pdf"

    last_res = None
    for i in range(0, 20):
        candidate = f"{base}{ext}" if i == 0 else f"{base}_{i+1}{ext}"
        payload = {
            "name": candidate,
            "parentReference": {"id": dest_folder_id},
        }
        res = graph("PATCH", url, token, json=payload)
        last_res = res
        if res.status_code in (200, 201):
            return res.json()
        if res.status_code == 409:
            continue
        res.raise_for_status()

    # exhausted retries
    if last_res is not None:
        last_res.raise_for_status()
    raise RuntimeError("move_and_rename failed without response")


def get_item(token: str, item_id: str) -> dict:
    url = (
        f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}"
        "?$select=id,name,createdDateTime,lastModifiedDateTime,file,parentReference"
    )
    res = graph("GET", url, token)
    res.raise_for_status()
    return res.json()


def process_item(token: str, state: dict, it: dict, apply: bool) -> None:
    item_id = it["id"]
    name = it.get("name") or item_id
    created = it.get("createdDateTime") or ""

    # Note: we intentionally do NOT skip already-processed items here;
    # reprocess mode wants to re-run classification.

    local_pdf = TMP_DIR / name
    try:
        if local_pdf.exists():
            local_pdf.unlink()
        download_item(token, item_id, local_pdf)
        text = ocr_first_page(local_pdf)

        recipient = pick_recipient(text, name)
        firma = pick_firma(text, name)
        doc_type = pick_doc_type(text, name)
        date = pick_date(text, created, name)

        # Canonicalize some common company names (keep folder names short)
        if "volkswagen" in firma.lower():
            firma = "Volkswagen"

        # If the company folder already exists under this recipient, reuse it (prevents duplicates)
        firma = resolve_existing_firma_folder(token, recipient, firma)

        # sanitize company/doc type for filename
        firma_fn = re.sub(r"\s+", " ", firma).strip()
        firma_fn = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß \-_.()]+", "", firma_fn)
        doc_fn = re.sub(r"\s+", " ", doc_type).strip()
        doc_fn = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß \-_.()]+", "", doc_fn)

        new_name = f"{date}_{firma_fn}_{doc_fn}.pdf"
        new_name = re.sub(r"\s+", " ", new_name).strip()

        # Folder routing rules
        if doc_type == "Kaufvertrag":
            # Per your preference: keep contracts under the company folder
            dest_path = f"{TARGET_ROOT}/Tim/{firma}"
        else:
            dest_path = f"{TARGET_ROOT}/{recipient}/{firma}"

        log.info("--- %s", name)
        log.info("  id:        %s", item_id)
        log.info("  recipient: %s", recipient)
        log.info("  firma:     %s", firma)
        log.info("  date:      %s", date)
        log.info("  type:      %s", doc_type)
        log.info("  dest:      %s", dest_path)
        log.info("  new_name:  %s", new_name)

        if apply:
            # ensure folder chain
            ensure_folder(token, TARGET_ROOT)
            ensure_folder(token, f"{TARGET_ROOT}/{recipient}")
            folder_id = ensure_folder(token, dest_path)
            move_and_rename(token, item_id, folder_id, new_name)
            state["processed"][item_id] = {
                "src": name,
                "dest": dest_path,
                "new": new_name,
                "ts": time.time(),
            }
            save_state(state)
            log.info("  APPLIED")
        else:
            log.info("  DRY_RUN")

    finally:
        try:
            if local_pdf.exists():
                local_pdf.unlink()
        except OSError:
            pass


def main(apply: bool = False, reprocess_ids: list[str] | None = None):
    token = get_token()
    state = load_state()

    TMP_DIR.mkdir(exist_ok=True)

    if reprocess_ids:
        for item_id in reprocess_ids:
            it = get_item(token, item_id)
            process_item(token, state, it, apply=apply)
        return

    pdfs = list_inbox_pdfs(token)
    if not pdfs:
        log.info("No PDFs found in inbox '%s'.", INBOX)
        return

    for it in pdfs:
        item_id = it["id"]
        if state["processed"].get(item_id):
            continue
        process_item(token, state, it, apply=apply)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually move/rename in OneDrive")
    ap.add_argument(
        "--reprocess",
        action="append",
        default=[],
        help="Reprocess a specific OneDrive item id (can be used multiple times).",
    )
    args = ap.parse_args()
    main(apply=args.apply, reprocess_ids=args.reprocess or None)
