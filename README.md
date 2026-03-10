# SortmyPDFs

Automatisches Einsammeln, OCR, Umbenennen und Einsortieren von PDF-Dokumenten aus **OneDrive** (z.B. Scans aus einem „vomDrucker“-Ordner) in eine saubere Zielstruktur.

Repo: https://github.com/rinkelzz/SortmyPDFs

---

## Was macht das Tool?

Typischer Workflow:
1. Dein Scanner/Drucker (oder ein IMAP-Ingest) legt PDFs im OneDrive-Inbox-Ordner ab (Standard: `vomDrucker`).
2. SortmyPDFs rendert **Seite 1** (Poppler), macht **OCR** (Tesseract) und extrahiert Hinweise.
3. Es erkennt bzw. schätzt:
   - **Empfänger** (z.B. `Tim`, `Chantal`, `Sonstige`)
   - **Firma/Absender**
   - **Dokumenttyp** (Kurztyp)
   - **Datum**
4. Danach wird die Datei nach `/SortmyPDFs/<Empfaenger>/<Firma>/` verschoben und benannt.

Namensschema (aktuell):
- Zielordner: `/SortmyPDFs/<Empfaenger>/<Firma>/`
- Dateiname: `YYYY-MM-DD_<Firma>_<Kurztyp>.pdf`
  - Datum = Datum im Dokument; Fallback: OneDrive `createdDateTime`

---

## Projektstruktur

Der eigentliche Code liegt unter:
- `SortmyPDFs/`
  - `sort_and_move.py` (Hauptlauf: sortieren/umbenennen/verschieben)
  - `auth_device_code.py` (OneDrive/Graph Device Code Flow)
  - `imap_ingest.py` (optional: PDFs aus IMAP nach OneDrive-Inbox)
  - `dashboard/` (optional: Web-Dashboard)

Zusätzliche Details/Notizen sind auch hier dokumentiert:
- `SortmyPDFs/README.md`

---

## Voraussetzungen

### System
- Linux (empfohlen/aktuell genutzt)
- Python 3.x

### System-Tools für OCR
Für die OCR/Extraktion von Seite 1:
- `pdftoppm` (Poppler)
- `tesseract`
- Sprachpaket `deu`

Ubuntu/Debian:
```bash
sudo apt update
sudo apt install -y poppler-utils tesseract-ocr tesseract-ocr-deu
```

### Python Dependencies
- für das Hauptprojekt: venv + pip
- für das Dashboard: extra Requirements unter `SortmyPDFs/dashboard/requirements.txt`

---

## Installation

### 1) Repo klonen
```bash
git clone https://github.com/rinkelzz/SortmyPDFs.git
cd SortmyPDFs
```

### 2) Python venv erstellen
Die venv liegt (wie im Projekt vorgesehen) im Unterordner `SortmyPDFs/.venv/`:
```bash
cd SortmyPDFs
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
```

Die benötigten Pakete kannst du jetzt direkt über die `requirements.txt` installieren (siehe oben). Für das Dashboard gibt es zusätzlich: `SortmyPDFs/dashboard/requirements.txt`.

---

## Konfiguration

### `.env` anlegen
Im Ordner `SortmyPDFs/`:
```bash
cp .env.example .env
```
Dann `.env` befüllen (Client-ID, Inbox-Ordner etc.).

### OneDrive / Microsoft Graph Auth
Es wird **Device Code Flow** verwendet.

Benötigt:
- App Registration (Client ID), die **Consumer Accounts** unterstützt
- Scopes mindestens: `Files.ReadWrite.All`

Einmalige Anmeldung:
```bash
cd SortmyPDFs
source .venv/bin/activate
python auth_device_code.py
```

---

## Nutzung

### Inbox testen
```bash
python graph_list_inbox.py
```

### Sortieren (Dry-Run)
```bash
python sort_and_move.py
```

### Sortieren (Apply / wirklich verschieben & umbenennen)
```bash
python sort_and_move.py --apply
```

---

## Optional: IMAP → OneDrive Inbox

Wenn du Dokumente per Mail sammelst:
1. IMAP Variablen in `.env` setzen (siehe `.env.example`)
2. Run (nur UNSEEN):
```bash
python imap_ingest.py
```
3. Run + nach Erfolg löschen:
```bash
python imap_ingest.py --delete
```
4. Alle Mails verarbeiten:
```bash
python imap_ingest.py --all --delete
```

---

## Dashboard (optional)

Das Dashboard liegt unter `SortmyPDFs/dashboard/`.

Start lokal:
```bash
cd SortmyPDFs
source .venv/bin/activate
pip install -r dashboard/requirements.txt
uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
```

Optional (Inbox live via Graph abfragen):
```bash
SORTMYPDFS_DASH_LIVE_INBOX=1 uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
```

LAN + Buttons + Basic Auth (empfohlen, wenn `--host 0.0.0.0`):
```bash
SORTMYPDFS_DASH_USER=deinuser \
SORTMYPDFS_DASH_PASS=deinpass \
SORTMYPDFS_DASH_BUTTONS=1 \
uvicorn dashboard.app:app --host 0.0.0.0 --port 8080
```

---

## Automatikbetrieb (systemd --user)

Im Projekt existiert ein Runner:
- `SortmyPDFs/run_hourly.sh`
  - macht i.d.R. `imap_ingest.py --delete` und anschließend `sort_and_move.py --apply`
  - schreibt Logs nach `SortmyPDFs/logs/`

Eine Beispielkonfiguration (Service+Timer) findest du ausführlich in:
- `SortmyPDFs/README.md`

---

## State / Duplikate / temporäre Dateien

- `SortmyPDFs/state.json`: bereits verarbeitete OneDrive-Dateien
- `SortmyPDFs/state_imap.json`: verarbeitete IMAP-UIDs + Attachment-Hashes
- `SortmyPDFs/.tmp/`: temporäre OCR-Artefakte

---

## Troubleshooting (kurz)

- **OCR fehlt / schlechte Erkennung**: prüfen, ob `pdftoppm` und `tesseract` installiert sind.
- **Graph Auth**: Device-Code erneut ausführen: `python auth_device_code.py`
- **IMAP SSL Hostname mismatch**: `IMAP_HOST` passt nicht zum Zertifikat (SAN/CN).

---

## Lizenz

Noch nicht festgelegt (aktuell „all rights reserved“ / privat). Wenn du willst, kann ich eine passende Lizenz (MIT/Apache-2.0/GPLv3) vorschlagen und hinzufügen.
