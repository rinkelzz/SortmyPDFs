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
  - `web_ingest.py` (optional: PDFs aus Web-Portalen per Playwright nach OneDrive-Inbox)
  - `web_portals.json` (Konfiguration der Web-Portale, aus `web_portals.example.json` kopieren)
  - `firma_aliases.json` (Firmen-Alias-Zuordnungen für die Erkennung)
  - `dashboard/` (optional: Web-Dashboard)
- `tests/` (Unit-Tests für die Erkennungslogik)

Zusätzliche Details/Notizen sind auch hier dokumentiert:
- `SortmyPDFs/README.md`

---

## Voraussetzungen

### System
- Linux (empfohlen/aktuell genutzt)
- Python 3.x

### System-Tools für OCR und Web-Automation
Für die OCR/Extraktion von Seite 1:
- `pdftoppm` (Poppler)
- `tesseract`
- Sprachpaket `deu`

Ubuntu/Debian:
```bash
sudo apt update
sudo apt install -y poppler-utils tesseract-ocr tesseract-ocr-deu
```

Für `web_ingest.py` zusätzlich Playwright-Browser:
```bash
# Nach pip install -r requirements.txt:
playwright install chromium
```

### Python Dependencies
- für das Hauptprojekt: venv + pip
- für das Dashboard: extra Requirements unter `SortmyPDFs/dashboard/requirements.txt`

---

## Installation

### Schnellstart (empfohlen)

Nach dem Klonen einfach den Installer ausführen – er erledigt alles automatisch:

```bash
git clone https://github.com/rinkelzz/SortmyPDFs.git
cd SortmyPDFs
chmod +x install.sh
./install.sh
```

Der Installer führt dich durch:
- Systemabhängigkeiten installieren (`poppler-utils`, `tesseract`)
- Python-venv + Pakete einrichten
- `.env` interaktiv befüllen (Graph Client-ID, IMAP etc.)
- systemd --user Units installieren (stündlicher Timer + Dashboard-Service)
- OneDrive-Einmalanmeldung (Device Code Flow) starten

**Optionen:**
```bash
./install.sh --no-dashboard   # Dashboard-Service überspringen
./install.sh --no-imap        # IMAP-Felder in .env überspringen
./install.sh --dry-run        # Nur anzeigen, was gemacht würde
```

---

### Manuell (Schritt für Schritt)

#### 1) Repo klonen
```bash
git clone https://github.com/rinkelzz/SortmyPDFs.git
cd SortmyPDFs
```

#### 2) Python venv erstellen
Die venv liegt im Unterordner `SortmyPDFs/.venv/`:
```bash
cd SortmyPDFs
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Für das Dashboard zusätzlich: `pip install -r dashboard/requirements.txt`.

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

## Optional: Web-Portal → OneDrive Inbox

`web_ingest.py` loggt sich automatisch in konfigurierte Web-Portale ein, lädt PDFs herunter und stellt sie in den OneDrive-Inbox. Intern wird Playwright (headless Chromium) verwendet.

### Einrichten

```bash
cp SortmyPDFs/web_portals.example.json SortmyPDFs/web_portals.json
```

Dann `web_portals.json` befüllen — pro Portal:
- `login_url`, `username_selector`, `password_selector`, `submit_selector`
- `documents_url` (URL der Dokumentenliste nach Login)
- `download_button_selector` für JS-getriggerte Downloads, oder `pdf_link_selector` für direkte `<a href="…pdf">`-Links
- Credentials über `.env`-Variablen (z.B. `WWK_USER`, `WWK_PASS`)

### Nutzung

```bash
python web_ingest.py                  # alle konfigurierten Portale
python web_ingest.py --portal WWK    # nur ein Portal
python web_ingest.py --list          # Dry-Run: Dokumente auflisten ohne Upload
python web_ingest.py --screenshot    # Screenshots für Diagnose speichern
```

### Aktuell konfiguriertes Portal: WWK

- **Login**: `https://my.wwk.de/` (OpenAM SSO → `https://sso.wwk.de/`)
- **Dokumente**: `https://wwkinside.wwk.de/group/portal/meine-dokumente`
- **Download**: JS-getriggerte Links (kein direktes PDF-href)
- **Credentials**: `WWK_USER` / `WWK_PASS` in `.env`
- **Deduplizierung**: per SHA-256-Hash in `state_web.json`

### Neues Portal hinzufügen

1. Eintrag in `web_portals.json` ergänzen (analog zum WWK-Beispiel)
2. Credentials in `.env` eintragen
3. Mit `--screenshot --list` testen — gespeicherte PNGs zeigen was der Browser sieht

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
- `SortmyPDFs/state_web.json`: hochgeladene Web-Portal-PDFs (SHA-256-Hashes)
- `SortmyPDFs/.tmp/`: temporäre OCR-Artefakte

---

## Erkennungslogik im Detail

Die Klassifikation in `sort_and_move.py` arbeitet rein regelbasiert (kein ML/KI) und nutzt OCR-Text + Dateiname als Eingabe.

### Empfänger (`pick_recipient`)

Prüft OCR-Text und Dateiname auf bekannte Vor-/Nachnamen. Reihenfolge (first match wins):

1. `"chantal"` im Text oder Dateiname → **Chantal**
2. `"tim"` + `"rinkel"` im Text, oder `"Herrn"` + `"rinkel"` → **Tim**
3. `"tim"` oder `"rinkel"` nur im Dateinamen → **Tim** (Fallback)
4. Sonst → **Sonstige**

### Firma/Absender (`pick_firma`)

Mehrstufige Erkennung mit folgender Priorität:

1. **Hardcoded Spezialfälle**: z.B. `HDI` wird immer sofort erkannt.
2. **Alias-Datei** (`firma_aliases.json`): Benutzerdefinierte Zuordnungen (Substring-Match, case-insensitive). Beispiel: `"Gothaer"` → `"Gothaer Allgemeine Versicherung"`.
3. **OCR-Heuristik**: Sucht in den ersten 25 Zeilen nach Organisationsmarkern wie `GmbH`, `AG`, `eG`, `Versicherung`, `Krankenkasse`, `Bank` etc.
4. **Dateiname**: Extrahiert den Firmennamen vor Schlüsselwörtern wie `Rechnung`, `Laborbefund`, `Kaufvertrag`.
5. **Fallback**: Erste 5 Wörter des Dateinamens oder `"Unbekannt"`.

Firmennamen werden über `firma_key()` normalisiert (case-insensitive, ohne Rechtsformen wie GmbH/AG), um Duplikate zu vermeiden. Existierende Ordner werden automatisch wiederverwendet.

### Dokumenttyp (`pick_doc_type`)

Einfaches Keyword-Matching (first match wins) in Text + Dateiname:

| Keyword              | Ergebnis           |
|----------------------|--------------------|
| `beitragsrechnung`   | Beitragsrechnung   |
| `rechnung`           | Rechnung           |
| `kaufvertrag`        | Kaufvertrag        |
| `laborbefund`        | Laborbefund        |
| `bescheid`           | Bescheid           |
| *(keins gefunden)*   | Dokument           |

### Datum (`pick_date`)

1. **OCR-Text**: Sammelt alle Daten im Format `DD.MM.YYYY` (auch `DD.MM.YY`), filtert vor 2010 und nach 2035 raus. Bevorzugt Daten nahe typischer Briefkopf-Muster (Komma, Stadtname im Kontext).
2. **Dateiname**: Falls der Name mit `YYYY-MM-DD` beginnt, wird das als starker Fallback genutzt.
3. **OneDrive `createdDateTime`**: Letzter Fallback.

### Tests

```bash
cd SortmyPDFs
python -m pytest tests/ -v
```

Die Tests decken alle oben genannten Erkennungsfunktionen ab (43 Tests).

---

## Troubleshooting (kurz)

- **OCR fehlt / schlechte Erkennung**: prüfen, ob `pdftoppm` und `tesseract` installiert sind.
- **Graph Auth**: Device-Code erneut ausführen: `python auth_device_code.py`
- **IMAP SSL Hostname mismatch**: `IMAP_HOST` passt nicht zum Zertifikat (SAN/CN).
- **web_ingest Login schlägt fehl**: `--screenshot` nutzen — `debug_<Portal>_login_page.png` zeigt was der Browser sieht. Selektoren in `web_portals.json` anpassen.
- **web_ingest 0 Dokumente**: `documents_url` in `web_portals.json` prüfen; mit `--screenshot` wird `debug_<Portal>_documents_page.png` gespeichert.

---

## Lizenz

MIT – siehe [LICENSE](LICENSE).
