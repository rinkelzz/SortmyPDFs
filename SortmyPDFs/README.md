# SortmyPDFs (aka „SortmyPDF“)

Ziel: PDFs in **OneDrive** automatisch einsammeln, **umbenennen** und in eine feste Ordnerstruktur **einsortieren**.

Typischer Use-Case:
- Scanner/Drucker speichert Scans als PDF in einen OneDrive-Eingangsordner (`vomDrucker`).
- Dieses Projekt liest die PDFs, macht (falls nötig) OCR auf Seite 1 und entscheidet daraus:
  - **Empfänger** (Tim/Chantal/Sonstige)
  - **Firma/Absender**
  - **Dokumenttyp** (Kurztyp)
  - **Datum**
- Anschließend wird die Datei verschoben nach `/SortmyPDFs/<Empfaenger>/<Firma>/` und sauber benannt.

## OneDrive Ordner & Namensschema (Regeln, aktuell)
- **Inbox-Ordner** (OneDrive Root): `vomDrucker`
- **Zielstruktur**: `/SortmyPDFs/<Empfaenger>/<Firma>/`
  - Empfaenger: `Chantal` | `Tim` | `Sonstige`
  - Firma: z.B. `HDI`
- **Dateiname**: `YYYY-MM-DD_<Firma>_<Kurztyp>.pdf`
  - Datum = Datum im Brief; falls nicht gefunden: OneDrive `createdDateTime`

## Voraussetzungen

### Python/Dependencies
- Python venv ist im Repo vorgesehen: `SortmyPDFs/.venv/`
- Python-Pakete (über `pip` in der venv): `msal`, `requests`, `python-dotenv`

### Systemtools (für OCR)
Für die Erkennung auf Seite 1 nutzt das Projekt:
- `pdftoppm` (Poppler) – rendert Seite 1 als PNG
- `tesseract` – OCR
- Sprachpaket: i.d.R. `deu`

Ohne diese Tools kann die Sortierung weniger gut werden (Dateiname/Datum/Firma werden dann häufiger aus Metadaten/Dateinamen geschätzt).

## OneDrive Auth (Personal)
Wir nutzen Microsoft Graph `device code flow`.

Du brauchst dafür eine **App Registration** (Client ID), die **Consumer Accounts** unterstützt.

Benötigte Scopes (mindestens):
- `Files.ReadWrite.All`

Hinweis: `offline_access`/`openid`/`profile` sind in MSAL (device code flow) reserviert und werden nicht als normale Scopes übergeben.

## Quickstart

### 0) `.env` anlegen
- Vorlage: `.env.example`
- Kopieren:
  - `cp .env.example .env`

### 1) venv aktivieren
- `cd /home/tim/.openclaw/workspace/SortmyPDFs`
- `source .venv/bin/activate`

### 2) OneDrive Auth (einmalig)
- `python auth_device_code.py`

### 3) Inbox testen
- `python graph_list_inbox.py`

### 4) Sortieren
- Dry-Run:
  - `python sort_and_move.py`
- Apply (verschiebt/benennt in OneDrive wirklich um):
  - `python sort_and_move.py --apply`

### 5) Optional: IMAP (E-Mail-Archiv) → OneDrive Inbox
Wenn du ein separates IMAP-Postfach als Sammelstelle für Dokumenten-Mails nutzt:
- `.env` befüllen (siehe `.env.example`, IMAP_* Variablen)
- Run (nur UNSEEN):
  - `python imap_ingest.py`
- Run + nach Erfolg löschen:
  - `python imap_ingest.py --delete`
- Alle Mails verarbeiten:
  - `python imap_ingest.py --all --delete`

## State / Duplikate / Temporäre Dateien
- `state.json`: bereits verarbeitete OneDrive-Dateien (damit nichts doppelt sortiert wird)
- `state_imap.json`: bereits verarbeitete IMAP-UIDs + SHA256-Hashes von Attachments
- `.tmp/`: temporäre OCR-Artefakte (gerenderte Seite 1 + OCR-Text)

## IMAP Setup Notes (E-Mail-Archiv-Postfach)
- Der IMAP-Ingest lädt PDF-Anhänge aus einem separaten Archiv-Postfach und legt sie in `vomDrucker/` ab.
- Danach sortiert `sort_and_move.py` wie gewohnt nach `/SortmyPDFs/<Empfaenger>/<Firma>/`.

## Dashboard (Status-Webseite)

Ein Dashboard ist enthalten unter `SortmyPDFs/dashboard/`.

Es zeigt u.a.:
- Warteschlange (optional live via Graph API)
- Gesamt verarbeitet (`state.json`)
- „Sonstige“-Anteil
- letzte Runs + Fehler (aus `logs/hourly-*.log`)

### Start (lokal)
- `cd /home/tim/.openclaw/workspace/SortmyPDFs`
- `source .venv/bin/activate`
- `pip install -r dashboard/requirements.txt`
- `uvicorn dashboard.app:app --host 127.0.0.1 --port 8080`

Optional (zeigt die Inbox/Warteschlange live via Graph API):
- `SORTMYPDFS_DASH_LIVE_INBOX=1 uvicorn dashboard.app:app --host 127.0.0.1 --port 8080`

### Heimnetz (LAN) + Buttons + Basic Auth (empfohlen)

**Wichtig:** Wenn du das Dashboard im Heimnetz bindest (`--host 0.0.0.0`) und Buttons aktivierst, setz **mindestens** Basic Auth.

Start (manuell):
- `SORTMYPDFS_DASH_USER=deinuser SORTMYPDFS_DASH_PASS=deinpass SORTMYPDFS_DASH_BUTTONS=1 uvicorn dashboard.app:app --host 0.0.0.0 --port 8080`

#### Dauerhaft als systemd --user Service (so ist es aktuell eingerichtet)

1) Env-Datei für Credentials/Flags anlegen:

`~/.config/sortmypdfs-dashboard.env`
```bash
# Set credentials (recommended when binding to LAN)
SORTMYPDFS_DASH_USER=change-me
SORTMYPDFS_DASH_PASS=change-me

# Enable action buttons (run now / timer on/off)
SORTMYPDFS_DASH_BUTTONS=1

# Optional: query OneDrive inbox live (requires Graph auth cache)
# SORTMYPDFS_DASH_LIVE_INBOX=1
```

2) systemd Unit:

`~/.config/systemd/user/sortmypdfs-dashboard.service`
```ini
[Unit]
Description=SortmyPDFs Dashboard (LAN)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/tim/.openclaw/workspace/SortmyPDFs
EnvironmentFile=/home/tim/.config/sortmypdfs-dashboard.env
ExecStart=/home/tim/.openclaw/workspace/SortmyPDFs/.venv/bin/uvicorn dashboard.app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

3) Aktivieren/Starten:
```bash
systemctl --user daemon-reload
systemctl --user enable --now sortmypdfs-dashboard.service
```

4) Status/Logs:
```bash
systemctl --user status sortmypdfs-dashboard.service
journalctl --user -u sortmypdfs-dashboard.service -n 200 --no-pager
```

#### URL im Heimnetz (lokale IP)
Die IP findest du z.B. so:
```bash
hostname -I
```
Dann ist die URL:
- `http://<lokale-ip>:8080`

## Automatikbetrieb (stündlich) mit Logs (systemd --user)

Es gibt ein Runner-Script im Repo:
- `run_hourly.sh`
  - macht: `imap_ingest.py --delete` (UNSEEN + nach Erfolg löschen) und danach `sort_and_move.py --apply`
  - schreibt pro Lauf ein Log nach: `logs/hourly-<UTC-Timestamp>.log`

### systemd Unit Dateien
Lege folgende Dateien an:

`~/.config/systemd/user/sortmypdfs.service`
```ini
[Unit]
Description=SortmyPDFs hourly ingest+sort (IMAP -> OneDrive -> rename/move)

[Service]
Type=oneshot
WorkingDirectory=/home/tim/.openclaw/workspace/SortmyPDFs
ExecStart=/home/tim/.openclaw/workspace/SortmyPDFs/run_hourly.sh
```

`~/.config/systemd/user/sortmypdfs.timer`
```ini
[Unit]
Description=Run SortmyPDFs every hour

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

Aktivieren:
```bash
systemctl --user daemon-reload
systemctl --user enable --now sortmypdfs.timer
```

Status/Debug:
```bash
systemctl --user status sortmypdfs.timer
systemctl --user -u sortmypdfs.service --no-pager -n 200
ls -lt /home/tim/.openclaw/workspace/SortmyPDFs/logs | head
```

### Konfiguration (`.env`)
Benötigte Variablen:
- `IMAP_HOST`
- `IMAP_PORT` (typisch 993)
- `IMAP_USER`
- `IMAP_PASSWORD`
- `IMAP_FOLDER` (typisch `INBOX`)

### Troubleshooting
- **SSL Hostname mismatch:** Wenn `ssl.SSLCertVerificationError: Hostname mismatch` kommt, stimmt `IMAP_HOST` nicht zum Zertifikat (SAN/CN) auf Port 993.
- **Delete Flag:** Das Script nutzt das System-Flag `\\Deleted` (ein Backslash). Manche Server akzeptieren keine abweichenden Schreibweisen.
