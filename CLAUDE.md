# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository

GitHub: https://github.com/rinkelzz/SortmyPDFs

## Setup

System dependencies (required for OCR and web automation):
```bash
sudo apt install -y poppler-utils tesseract-ocr tesseract-ocr-deu
```

Python environment:
```bash
cd SortmyPDFs
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium   # one-time browser install for web_ingest.py
```

## Commands

```bash
# Run tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_sort_and_move.py -v

# Dry-run sorting (no changes)
python sort_and_move.py

# Actually sort/rename/move files in OneDrive
python sort_and_move.py --apply

# One-time OneDrive OAuth setup
python auth_device_code.py

# List inbox contents (debug)
python graph_list_inbox.py

# Email harvester (download PDF attachments from IMAP)
python imap_ingest.py
python imap_ingest.py --delete

# Web portal harvester (download PDFs from portals with login)
python web_ingest.py                     # all configured portals
python web_ingest.py --portal WWK        # one portal
python web_ingest.py --debug             # visible browser (for debugging/setup)
python web_ingest.py --list              # list found PDFs without uploading

# Dashboard
pip install -r dashboard/requirements.txt
uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
```

## Architecture

The core pipeline in `sort_and_move.py`:
1. Lists PDFs in the OneDrive inbox folder (`vomDrucker` by default)
2. Downloads each PDF, renders page 1 to PNG, runs Tesseract OCR
3. Classifies via rule-based functions: `pick_recipient()`, `pick_firma()`, `pick_doc_type()`, `pick_date()`
4. Moves/renames to `SortmyPDFs/<Recipient>/<Company>/YYYY-MM-DD_<Company>_<Type>.pdf` via Microsoft Graph API
5. Tracks processed file IDs in `state.json` to prevent reprocessing

**Classification is entirely rule-based** (keyword matching, regex, string heuristics — no ML). Company name extraction uses a multi-strategy cascade: hardcoded cases → `firma_aliases.json` → OCR heuristics → filename fallback.

`firma_aliases.json` maps canonical company names to OCR substring triggers:
```json
{"Canonical Name": ["substring1", "substring2"]}
```
Case-insensitive substring matching against OCR text.

`imap_ingest.py` is an optional preprocessor that downloads PDF attachments from a mailbox and uploads them to the OneDrive inbox for the main pipeline to process. It deduplicates via `state_imap.json`.

`web_ingest.py` is an optional preprocessor that uses Playwright (headless Chromium) to log into web portals, find PDF documents, download them, and upload to the OneDrive inbox. Portals are configured in `web_portals.json` (copy from `web_portals.example.json`). Each portal entry defines CSS selectors for the login form and document links. Credentials come from `.env`. Deduplicates via SHA-256 hash in `state_web.json`. Currently configured portal: **WWK** (OpenAM SSO at `https://my.wwk.de/`). For portals where PDF links are JavaScript-triggered (not `<a href>`), use `download_button_selector` instead of `pdf_link_selector`.

`dashboard/app.py` (FastAPI) provides a web UI to view inbox status, processing history, manage `firma_aliases.json`, and trigger runs manually.

## Configuration

All configuration lives in `SortmyPDFs/.env` (see `.env.example`). Key variables:
- `ONEDRIVE_INBOX` — source folder name in OneDrive (default: `vomDrucker`)
- `ONEDRIVE_TARGET_ROOT` — output root folder (default: `SortmyPDFs`)
- `GRAPH_CLIENT_ID` — Azure App Registration ID
- `IMAP_*` — optional email harvesting credentials
- `WWK_USER` / `WWK_PASS` — WWK portal credentials (and analogous vars for other portals)

OAuth token is cached in `.token_cache.bin`. Run `auth_device_code.py` to (re-)authenticate.

## Automated Operation

Hourly automation uses systemd user units (`~/.config/systemd/user/`):
- `sortmypdfs.service` + `sortmypdfs.timer` — runs `run_hourly.sh` hourly
- `sortmypdfs-dashboard.service` — optional persistent dashboard

Logs written to `SortmyPDFs/logs/hourly-<timestamp>.log`.
