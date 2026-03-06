import os
import re
import json
import time
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import msal
import requests
from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

TENANT = os.getenv("GRAPH_TENANT", "consumers")
CLIENT_ID = os.getenv("GRAPH_CLIENT_ID")
SCOPES = os.getenv("GRAPH_SCOPES", "Files.ReadWrite.All").split()
INBOX = os.getenv("ONEDRIVE_INBOX", "vomDrucker")
TARGET_ROOT = os.getenv("ONEDRIVE_TARGET_ROOT", "SortmyPDFs")

STATE_PATH = BASE / "state.json"
TOKEN_CACHE_PATH = BASE / ".token_cache.bin"
TMP_DIR = BASE / ".tmp"

SESSION = requests.Session()


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"processed": {}}


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def get_app():
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


def get_token():
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


def graph(method, url, token, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Accept", "application/json")
    return SESSION.request(method, url, headers=headers, timeout=60, **kwargs)


def list_inbox_pdfs(token):
    url = (
        f"https://graph.microsoft.com/v1.0/me/drive/root:/{INBOX}:/children"
        "?$select=id,name,createdDateTime,lastModifiedDateTime,file"
    )
    res = graph("GET", url, token)
    res.raise_for_status()
    items = res.json().get("value", [])
    return [it for it in items if it.get("file") and it["name"].lower().endswith(".pdf")]


def download_item(token, item_id, out_path: Path):
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


def ocr_first_page(pdf_path: Path) -> str:
    TMP_DIR.mkdir(exist_ok=True)
    stem = TMP_DIR / (pdf_path.stem + "-page")
    # render first page to png
    subprocess.run([
        "pdftoppm",
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
    img = Path(str(stem) + "-1.png")
    out_base = TMP_DIR / (pdf_path.stem + "-ocr")
    subprocess.run([
        "tesseract",
        str(img),
        str(out_base),
        "-l",
        "deu",
        "--psm",
        "6",
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    txt_path = Path(str(out_base) + ".txt")
    return txt_path.read_text(encoding="utf-8", errors="ignore")


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


def pick_firma(text: str, fallback_name: str) -> str:
    # Always prefer HDI if present (your rule)
    if re.search(r"\bHDI\b", text):
        return "HDI"
    if "hdi" in fallback_name.lower():
        return "HDI"

    # Use filename as a strong signal (usually cleaner than OCR)
    from_fn = firma_from_filename(fallback_name)

    # OCR heuristic: first plausible org-like line in first ~20 lines, but strip addresses
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    head = lines[:25]
    org_re = re.compile(r"\b(GmbH|AG|eG|Versicherung|Krankenkasse|Bank|Finanzdienstleistungen|Kirchenamt)\b")

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
    mfn = re.match(r"^(\d{4})-(\d{2})-(\d{2})\b", fallback_name)
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
    except Exception:
        return datetime.utcnow().strftime("%Y-%m-%d")


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


def move_and_rename(token, item_id: str, dest_folder_id: str, new_name: str):
    url = f"https://graph.microsoft.com/v1.0/me/drive/items/{item_id}"
    payload = {
        "name": new_name,
        "parentReference": {"id": dest_folder_id},
    }
    res = graph("PATCH", url, token, json=payload)
    res.raise_for_status()
    return res.json()


def main(apply: bool = False):
    token = get_token()
    state = load_state()
    pdfs = list_inbox_pdfs(token)
    if not pdfs:
        print(f"No PDFs found in inbox '{INBOX}'.")
        return

    TMP_DIR.mkdir(exist_ok=True)

    for it in pdfs:
        item_id = it["id"]
        name = it["name"]
        created = it.get("createdDateTime") or ""

        if state["processed"].get(item_id):
            continue

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

            # sanitize company/doc type for filename
            firma_fn = re.sub(r"\s+", " ", firma).strip()
            firma_fn = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß \-_.()]+", "", firma_fn)
            doc_fn = re.sub(r"\s+", " ", doc_type).strip()
            doc_fn = re.sub(r"[^A-Za-z0-9ÄÖÜäöüß \-_.()]+", "", doc_fn)

            new_name = f"{date}_{firma_fn}_{doc_fn}.pdf"
            new_name = re.sub(r"\s+", " ", new_name).strip()

            # Folder routing rules
            if doc_type == "Kaufvertrag":
                # Per your preference: keep contracts under Tim/Sonstiges (but keep company in filename)
                dest_path = f"{TARGET_ROOT}/Tim/Sonstiges"
            else:
                dest_path = f"{TARGET_ROOT}/{recipient}/{firma}"

            print("---")
            print("src:", name)
            print("recipient:", recipient)
            print("firma:", firma)
            print("date:", date)
            print("type:", doc_type)
            print("dest:", dest_path)
            print("new:", new_name)

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
                print("APPLIED")
            else:
                print("DRY_RUN")

        finally:
            try:
                if local_pdf.exists():
                    local_pdf.unlink()
            except Exception:
                pass


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually move/rename in OneDrive")
    args = ap.parse_args()
    main(apply=args.apply)
