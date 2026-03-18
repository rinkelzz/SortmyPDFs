"""web_ingest.py – Download PDFs from web portals and upload to OneDrive inbox.

Each portal is defined in web_portals.json. Credentials are read from .env.
State is tracked in state_web.json (by SHA-256 hash) to avoid re-uploading.

Usage:
    python web_ingest.py                        # all portals
    python web_ingest.py --portal WWK           # one portal
    python web_ingest.py --debug                # visible browser (for debugging)
    python web_ingest.py --list                 # list found PDFs without uploading
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, Page, Download, TimeoutError as PWTimeout

from sort_and_move import get_token
from imap_ingest import upload_pdf_to_inbox

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

PORTALS_PATH = BASE / "web_portals.json"
STATE_PATH = BASE / "state_web.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("web_ingest")


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"processed_hashes": {}}


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(STATE_PATH)


# ---------------------------------------------------------------------------
# Portal config
# ---------------------------------------------------------------------------

def load_portals() -> list[dict]:
    if not PORTALS_PATH.exists():
        raise SystemExit(
            f"{PORTALS_PATH} not found. Create it from web_portals.example.json."
        )
    return json.loads(PORTALS_PATH.read_text(encoding="utf-8"))


def resolve_credentials(portal: dict) -> tuple[str, str]:
    user_env = portal.get("username_env", "")
    pass_env = portal.get("password_env", "")
    username = os.getenv(user_env, "")
    password = os.getenv(pass_env, "")
    if not username or not password:
        raise SystemExit(
            f"Portal '{portal['name']}': missing credentials. "
            f"Set {user_env} and {pass_env} in .env"
        )
    return username, password


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def _safe_name(name: str) -> str:
    name = name.strip()
    name = re.sub(r"[\\/:*?\"<>|]", "-", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "dokument"


def login(page: Page, portal: dict) -> None:
    """Navigate to login URL, fill credentials, submit, wait for post-login page."""
    username, password = resolve_credentials(portal)

    log.info("[%s] Navigating to %s", portal["name"], portal["login_url"])
    page.goto(portal["login_url"], wait_until="networkidle", timeout=30_000)
    log.info("[%s] Landed on: %s", portal["name"], page.url)

    # Always screenshot before trying to interact, helps diagnose selector issues
    _screenshot(page, portal, "login_page")

    # Dismiss cookie consent if present (try text-based locator first, then CSS fallbacks)
    try:
        btn = page.locator("button", has_text="OK").first
        btn.wait_for(state="visible", timeout=5_000)
        btn.click()
        log.info("[%s] Cookie consent dismissed", portal["name"])
        page.wait_for_timeout(500)
    except Exception:
        consent_sel = portal.get("cookie_consent_selector", "")
        if consent_sel:
            for sel in [s.strip() for s in consent_sel.split(",")]:
                try:
                    page.wait_for_selector(sel, timeout=2_000)
                    page.click(sel)
                    log.info("[%s] Cookie consent dismissed via '%s'", portal["name"], sel)
                    page.wait_for_timeout(500)
                    break
                except Exception:
                    continue
        else:
            log.debug("[%s] No cookie consent banner found", portal["name"])

    # Fill username — support comma-separated fallback selectors
    username_sel = portal["username_selector"].split(",")[0].strip()
    for sel in [s.strip() for s in portal["username_selector"].split(",")]:
        try:
            page.wait_for_selector(sel, timeout=5_000)
            username_sel = sel
            break
        except Exception:
            continue
    else:
        raise RuntimeError(f"[{portal['name']}] Username field not found with selectors: {portal['username_selector']}")
    page.fill(username_sel, username)

    # Fill password — pick first matching selector
    password_sel = portal["password_selector"].split(",")[0].strip()
    for sel in [s.strip() for s in portal["password_selector"].split(",")]:
        try:
            page.wait_for_selector(sel, timeout=3_000)
            password_sel = sel
            break
        except Exception:
            continue
    page.fill(password_sel, password)

    # Submit — pick first matching selector
    submit_sel = portal["submit_selector"].split(",")[0].strip()
    for sel in [s.strip() for s in portal["submit_selector"].split(",")]:
        try:
            page.wait_for_selector(sel, timeout=3_000)
            submit_sel = sel
            break
        except Exception:
            continue
    page.click(submit_sel)

    # Wait for post-login navigation/confirmation
    try:
        page.wait_for_url(lambda url: "#login" not in url, timeout=20_000)
    except PWTimeout:
        pass  # may still have navigated; log URL below
    page.wait_for_load_state("networkidle", timeout=20_000)

    log.info("[%s] Login complete. URL: %s", portal["name"], page.url)


def collect_pdfs(page: Page, portal: dict) -> list[dict]:
    """
    Navigate to documents_url and return list of {name, href|click_selector} dicts.
    Supports:
      - pdf_link_selector: CSS selector for <a href="...pdf"> direct links
      - download_button_selector: CSS selector for buttons that trigger browser downloads
    """
    docs_url = portal.get("documents_url")
    if docs_url:
        log.info("[%s] Navigating to documents page: %s", portal["name"], docs_url)
        page.goto(docs_url, wait_until="networkidle", timeout=30_000)
    else:
        # Try clicking a nav link to get to the documents page
        nav_sel = portal.get("documents_nav_selector")
        if nav_sel:
            for sel in [s.strip() for s in nav_sel.split(",")]:
                try:
                    page.wait_for_selector(sel, timeout=5_000)
                    page.click(sel)
                    page.wait_for_load_state("networkidle", timeout=15_000)
                    log.info("[%s] Navigated to documents via '%s' → %s", portal["name"], sel, page.url)
                    break
                except Exception:
                    continue

    found: list[dict] = []

    # Strategy 1: direct PDF hrefs
    link_sel = portal.get("pdf_link_selector")
    if link_sel:
        links = page.query_selector_all(link_sel)
        for link in links:
            href = link.get_attribute("href") or ""
            text = (link.inner_text() or "").strip() or "dokument"
            name = _safe_name(text)
            if href:
                found.append({"type": "href", "name": name, "href": href, "element": link})

    # Strategy 2: download-trigger buttons (JS-triggered downloads)
    btn_sel = portal.get("download_button_selector")
    if btn_sel:
        buttons = page.query_selector_all(btn_sel)
        for i, btn in enumerate(buttons):
            text = (btn.inner_text() or "").strip()
            if len(text) <= 1:  # skip pagination links (F, P, 1, 2, 3, N, E)
                continue
            name = _safe_name(text) or f"dokument_{i+1}"
            # Store text for re-finding after page refresh, not the stale element handle
            found.append({"type": "download_trigger", "name": name, "trigger_text": text, "trigger_sel": btn_sel})

    log.info("[%s] Found %d document(s)", portal["name"], len(found))
    return found


def fetch_pdf_bytes(page: Page, portal: dict, doc: dict) -> tuple[str, bytes] | None:
    """Download a single PDF and return (filename, bytes)."""
    name = doc["name"]
    if not name.lower().endswith(".pdf"):
        name += ".pdf"

    if doc["type"] == "href":
        href = doc["href"]
        # Resolve relative URLs
        if href.startswith("/"):
            from urllib.parse import urlparse
            parsed = urlparse(page.url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        try:
            with page.expect_download(timeout=30_000) as dl_info:
                page.goto(href)
            dl: Download = dl_info.value
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                dl.save_as(f.name)
                data = Path(f.name).read_bytes()
            Path(f.name).unlink(missing_ok=True)
            return dl.suggested_filename or name, data
        except PWTimeout:
            # Not a download, read page content as fallback
            pass
        # Direct fetch via requests (same session cookies not available here — skip for now)
        log.warning("[%s] Could not download '%s' via href, skipping", portal["name"], name)
        return None

    elif doc["type"] == "download_trigger":
        try:
            # Re-find element fresh (stale after previous download/page refresh)
            trigger_text = doc.get("trigger_text", "")
            btn_sel = doc.get("trigger_sel", "")
            element = None
            if trigger_text and btn_sel:
                for el in page.query_selector_all(btn_sel):
                    if (el.inner_text() or "").strip() == trigger_text:
                        element = el
                        break
            if element is None:
                log.warning("[%s] Could not re-find element for '%s', skipping", portal["name"], name)
                return None
            with page.expect_download(timeout=30_000) as dl_info:
                element.click()
            dl: Download = dl_info.value
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
                dl.save_as(f.name)
                data = Path(f.name).read_bytes()
            Path(f.name).unlink(missing_ok=True)
            return dl.suggested_filename or name, data
        except PWTimeout:
            log.warning("[%s] Download trigger timed out for '%s', skipping", portal["name"], name)
            return None

    return None


# ---------------------------------------------------------------------------
# Main processing
# ---------------------------------------------------------------------------

def _screenshot(page: Page, portal: dict, label: str) -> None:
    path = BASE / f"debug_{portal['name']}_{label}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        log.info("[%s] Screenshot saved: %s", portal["name"], path)
    except Exception as e:
        log.warning("[%s] Screenshot failed: %s", portal["name"], e)


def process_portal(portal: dict, token: str, state: dict, dry_run: bool, screenshot: bool) -> int:
    """Process a single portal. Returns number of uploaded PDFs."""
    processed_hashes: dict = state.setdefault("processed_hashes", {})
    uploaded = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        try:
            login(page, portal)
            if screenshot:
                _screenshot(page, portal, "after_login")
                log.info("[%s] Current URL after login: %s", portal["name"], page.url)

            docs = collect_pdfs(page, portal)
            if screenshot:
                _screenshot(page, portal, "documents_page")
                log.info("[%s] Documents page URL: %s", portal["name"], page.url)

            if not docs:
                log.info("[%s] No documents found.", portal["name"])
                return 0

            for doc in docs:
                result = fetch_pdf_bytes(page, portal, doc)
                if result is None:
                    continue
                filename, data = result

                h = hashlib.sha256(data).hexdigest()
                if h in processed_hashes:
                    log.info("[%s] Already processed: %s (skipping)", portal["name"], filename)
                    continue

                if dry_run:
                    log.info("[%s] [DRY-RUN] Would upload: %s (%d bytes)", portal["name"], filename, len(data))
                else:
                    upload_pdf_to_inbox(token, filename, data)
                    processed_hashes[h] = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "filename": filename,
                        "portal": portal["name"],
                    }
                    log.info("[%s] Uploaded: %s", portal["name"], filename)
                    uploaded += 1

        except Exception as e:
            log.error("[%s] Error: %s", portal["name"], e)
        finally:
            context.close()
            browser.close()

    return uploaded


def main() -> None:
    parser = argparse.ArgumentParser(description="Download PDFs from web portals and upload to OneDrive inbox.")
    parser.add_argument("--portal", help="Process only this portal (by name)")
    parser.add_argument("--screenshot", action="store_true", help="Save PNG screenshots after login (for debugging/setup)")
    parser.add_argument("--list", action="store_true", dest="dry_run", help="List found PDFs without uploading")
    args = parser.parse_args()

    portals = load_portals()
    if args.portal:
        portals = [p for p in portals if p["name"] == args.portal]
        if not portals:
            raise SystemExit(f"Portal '{args.portal}' not found in {PORTALS_PATH}")

    token = get_token()
    state = load_state()
    any_changes = False
    total = 0

    for portal in portals:
        n = process_portal(
            portal,
            token,
            state,
            dry_run=args.dry_run,
            screenshot=args.screenshot,
        )
        total += n
        if n > 0:
            any_changes = True
            save_state(state)  # save after each portal

    if not any_changes and not args.dry_run:
        log.info("No new documents found across %d portal(s).", len(portals))
    else:
        log.info("Done. Uploaded %d document(s).", total)


if __name__ == "__main__":
    main()
