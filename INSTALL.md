# Installation auf einem frischen Ubuntu Minimal System

Diese Anleitung beschreibt die komplette Einrichtung von SortmyPDFs auf einem frischen Ubuntu Minimal System (22.04 LTS oder neuer). Am Ende läuft das Tool stündlich automatisch, sortiert PDFs aus OneDrive und stellt optional ein Web-Dashboard bereit.

---

## Übersicht

Was wird installiert:

- **Systemtools**: Git, Python 3, pip, Poppler (PDF→Bild), Tesseract (OCR)
- **Python-Abhängigkeiten**: msal, requests, python-dotenv (+ optional FastAPI für Dashboard)
- **Microsoft Graph Auth**: Device Code Flow für OneDrive-Zugriff
- **Optional**: IMAP-Ingest (E-Mail-Anhänge → OneDrive), Web-Dashboard, systemd-Timer

Geschätzte Dauer: ca. 15–20 Minuten.

---

## Schritt 1: System aktualisieren

```bash
sudo apt update && sudo apt upgrade -y
```

---

## Schritt 2: System-Abhängigkeiten installieren

```bash
sudo apt install -y \
  git \
  python3 \
  python3-pip \
  python3-venv \
  poppler-utils \
  tesseract-ocr \
  tesseract-ocr-deu
```

**Was wird installiert und warum:**

| Paket               | Zweck                                                |
|----------------------|------------------------------------------------------|
| `git`                | Repo klonen                                          |
| `python3`            | Python-Laufzeit                                      |
| `python3-pip`        | Python-Paketmanager                                  |
| `python3-venv`       | Virtuelle Umgebungen                                 |
| `poppler-utils`      | Enthält `pdftoppm` – rendert PDF-Seite 1 als PNG     |
| `tesseract-ocr`      | OCR-Engine für Texterkennung                         |
| `tesseract-ocr-deu`  | Deutsches Sprachpaket für Tesseract                  |

**Prüfen ob alles da ist:**

```bash
python3 --version     # Python 3.10+
pdftoppm -v           # pdftoppm version 22.x+
tesseract --version   # tesseract 5.x+
```

---

## Schritt 3: Repo klonen

```bash
cd ~
git clone https://github.com/rinkelzz/SortmyPDFs.git
cd SortmyPDFs
```

---

## Schritt 4: Python Virtual Environment einrichten

```bash
cd ~/SortmyPDFs/SortmyPDFs
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**Prüfen:**

```bash
python -c "import msal; print('msal OK')"
python -c "import requests; print('requests OK')"
python -c "import dotenv; print('dotenv OK')"
```

---

## Schritt 5: Microsoft Azure App Registration

SortmyPDFs braucht Zugriff auf dein OneDrive. Dafür muss einmalig eine Azure App registriert werden.

### 5a) App Registration erstellen

1. Öffne [https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade](https://portal.azure.com/#blade/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. Klick **"Neue Registrierung"**
3. Einstellungen:
   - **Name**: `SortmyPDFs` (frei wählbar)
   - **Unterstützte Kontotypen**: **"Konten in einem beliebigen Organisationsverzeichnis und persönliche Microsoft-Konten"** (das ist wichtig für Consumer-OneDrive!)
   - **Umleitungs-URI**: leer lassen (nicht nötig für Device Code Flow)
4. Klick **"Registrieren"**
5. Auf der Übersichtsseite die **Anwendungs-ID (Client-ID)** kopieren — die brauchst du gleich.

### 5b) API-Berechtigungen setzen

1. Im linken Menü → **"API-Berechtigungen"**
2. Klick **"Berechtigung hinzufügen"** → **"Microsoft Graph"** → **"Delegierte Berechtigungen"**
3. Suche und aktiviere:
   - `Files.ReadWrite.All`
4. Klick **"Berechtigungen hinzufügen"**

> **Hinweis**: Für persönliche Microsoft-Konten (Consumer) ist kein Admin-Consent nötig. Die Berechtigungen werden beim ersten Login bestätigt.

### 5c) Device Code Flow aktivieren

1. Im linken Menü → **"Authentifizierung"**
2. Ganz unten: **"Öffentliche Clientflows zulassen"** → auf **"Ja"** setzen
3. **Speichern**

---

## Schritt 6: .env konfigurieren

```bash
cd ~/SortmyPDFs/SortmyPDFs
cp .env.example .env
nano .env
```

Mindestens diese Variablen setzen:

```bash
# === OneDrive / Microsoft Graph ===
GRAPH_CLIENT_ID=deine-client-id-aus-schritt-5
GRAPH_TENANT=consumers
GRAPH_SCOPES=Files.ReadWrite.All

# OneDrive-Ordner, in dem der Scanner/Drucker die PDFs ablegt
ONEDRIVE_INBOX=vomDrucker

# Ziel-Root-Ordner in OneDrive
ONEDRIVE_TARGET_ROOT=SortmyPDFs
```

Falls du auch IMAP nutzen willst (E-Mail-Anhänge einsammeln), zusätzlich:

```bash
# === IMAP (optional) ===
IMAP_HOST=imap.example.com
IMAP_PORT=993
IMAP_USER=dein@email.de
IMAP_PASSWORD=dein-passwort
IMAP_FOLDER=INBOX
```

Speichern mit `Ctrl+O`, `Enter`, `Ctrl+X`.

> **Falls `.env.example` nicht vorhanden ist**, erstelle die `.env` manuell mit den Variablen oben.

---

## Schritt 7: OneDrive authentifizieren

```bash
cd ~/SortmyPDFs/SortmyPDFs
source .venv/bin/activate
python auth_device_code.py
```

Es erscheint eine Meldung wie:

```
To sign in, use a web browser to open the page https://microsoft.com/devicelogin
and enter the code XXXXXXXXX to authenticate.
```

1. Öffne die URL in einem Browser (kann auch auf einem anderen Gerät sein)
2. Gib den angezeigten Code ein
3. Melde dich mit deinem Microsoft-Konto an und bestätige die Berechtigungen
4. Zurück im Terminal sollte stehen: `OK: token acquired.`

Der Token wird lokal in `.token_cache.bin` gespeichert und automatisch erneuert.

---

## Schritt 8: Testen

### 8a) Inbox prüfen

Lege testweise eine PDF-Datei in deinen OneDrive-Ordner `vomDrucker` und prüfe:

```bash
python graph_list_inbox.py
```

Erwartete Ausgabe:

```
Inbox 'vomDrucker': 1 items (1 pdfs)
- TestDokument.pdf  created=2025-01-15T10:30:00Z  id=ABC123...
```

### 8b) Dry-Run (simuliert, verschiebt nichts)

```bash
python sort_and_move.py
```

Erwartete Ausgabe (pro PDF):

```
---
id: ABC123...
src: TestDokument.pdf
recipient: Tim
firma: Muster GmbH
date: 2025-01-15
type: Rechnung
dest: SortmyPDFs/Tim/Muster GmbH
new: 2025-01-15_Muster GmbH_Rechnung.pdf
DRY_RUN
```

### 8c) Wirklich sortieren

Wenn der Dry-Run gut aussieht:

```bash
python sort_and_move.py --apply
```

Die PDF wird jetzt in OneDrive verschoben und umbenannt.

---

## Schritt 9: Unit-Tests ausführen (optional)

```bash
cd ~/SortmyPDFs
source SortmyPDFs/.venv/bin/activate
pip install pytest
python -m pytest tests/ -v
```

Alle 43 Tests sollten grün sein.

---

## Schritt 10: Automatikbetrieb mit systemd (optional)

Damit SortmyPDFs stündlich automatisch läuft:

### 10a) run_hourly.sh anpassen

```bash
nano ~/SortmyPDFs/SortmyPDFs/run_hourly.sh
```

Passe den `BASE`-Pfad an dein System an:

```bash
BASE="/home/DEIN_USER/SortmyPDFs/SortmyPDFs"
```

Ausführbar machen:

```bash
chmod +x ~/SortmyPDFs/SortmyPDFs/run_hourly.sh
```

### 10b) systemd Service-Datei erstellen

```bash
mkdir -p ~/.config/systemd/user
```

```bash
cat > ~/.config/systemd/user/sortmypdfs.service << 'EOF'
[Unit]
Description=SortmyPDFs hourly ingest+sort (IMAP -> OneDrive -> rename/move)

[Service]
Type=oneshot
WorkingDirectory=%h/SortmyPDFs/SortmyPDFs
ExecStart=%h/SortmyPDFs/SortmyPDFs/run_hourly.sh
EOF
```

### 10c) systemd Timer-Datei erstellen

```bash
cat > ~/.config/systemd/user/sortmypdfs.timer << 'EOF'
[Unit]
Description=Run SortmyPDFs every hour

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
EOF
```

### 10d) Aktivieren und starten

```bash
systemctl --user daemon-reload
systemctl --user enable --now sortmypdfs.timer
```

### 10e) Prüfen

```bash
# Timer-Status
systemctl --user status sortmypdfs.timer

# Manuell auslösen (zum Testen)
systemctl --user start sortmypdfs.service

# Logs anschauen
systemctl --user status sortmypdfs.service
ls -lt ~/SortmyPDFs/SortmyPDFs/logs/ | head
cat ~/SortmyPDFs/SortmyPDFs/logs/$(ls -t ~/SortmyPDFs/SortmyPDFs/logs/ | head -1)
```

### 10f) Damit der Timer auch ohne Login läuft

Standardmäßig werden `--user`-Services beendet, wenn du dich ausloggst. Um das zu verhindern:

```bash
sudo loginctl enable-linger $USER
```

---

## Schritt 11: Dashboard einrichten (optional)

Das Web-Dashboard zeigt den aktuellen Status, verarbeitete Dateien und ermöglicht das manuelle Auslösen von Sortiervorgängen.

### 11a) Dashboard-Abhängigkeiten installieren

```bash
cd ~/SortmyPDFs/SortmyPDFs
source .venv/bin/activate
pip install -r dashboard/requirements.txt
```

### 11b) Lokal testen

```bash
uvicorn dashboard.app:app --host 127.0.0.1 --port 8080
```

Öffne `http://localhost:8080` im Browser.

### 11c) Im Heimnetz mit Basic Auth (empfohlen)

Erstelle eine Env-Datei für die Dashboard-Credentials:

```bash
cat > ~/.config/sortmypdfs-dashboard.env << 'EOF'
SORTMYPDFS_DASH_USER=dein_username
SORTMYPDFS_DASH_PASS=dein_sicheres_passwort
SORTMYPDFS_DASH_BUTTONS=1
# Optional: OneDrive-Inbox live abfragen
# SORTMYPDFS_DASH_LIVE_INBOX=1
EOF
```

### 11d) Dashboard als systemd Service (dauerhaft)

```bash
cat > ~/.config/systemd/user/sortmypdfs-dashboard.service << 'EOF'
[Unit]
Description=SortmyPDFs Dashboard (LAN)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=%h/SortmyPDFs/SortmyPDFs
EnvironmentFile=%h/.config/sortmypdfs-dashboard.env
ExecStart=%h/SortmyPDFs/SortmyPDFs/.venv/bin/uvicorn dashboard.app:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now sortmypdfs-dashboard.service
```

Dashboard im Heimnetz erreichbar unter:

```bash
echo "http://$(hostname -I | awk '{print $1}'):8080"
```

---

## Zusammenfassung: Was läuft am Ende?

| Komponente              | Was passiert                                    | Wie oft       |
|-------------------------|-------------------------------------------------|---------------|
| **sortmypdfs.timer**    | IMAP-Ingest + PDF-Sortierung                    | Stündlich      |
| **Dashboard**           | Web-UI auf Port 8080 im LAN                     | Dauerhaft      |
| **Token-Refresh**       | MSAL erneuert den OneDrive-Token automatisch     | Bei jedem Lauf |

---

## Troubleshooting

### OCR liefert schlechte Ergebnisse

```bash
# Prüfen ob deutsche Sprache installiert ist
tesseract --list-langs | grep deu

# Falls nicht:
sudo apt install tesseract-ocr-deu
```

### Graph Auth abgelaufen

```bash
cd ~/SortmyPDFs/SortmyPDFs
source .venv/bin/activate
python auth_device_code.py
```

### Timer läuft nicht nach Logout

```bash
sudo loginctl enable-linger $USER
```

### IMAP SSL-Fehler

Wenn `ssl.SSLCertVerificationError: Hostname mismatch` kommt, stimmt `IMAP_HOST` in der `.env` nicht mit dem Zertifikat des Servers überein. Prüfe den tatsächlichen Hostnamen:

```bash
openssl s_client -connect imap.example.com:993 2>/dev/null | openssl x509 -noout -subject -ext subjectAltName
```

### Berechtigungsfehler bei OneDrive

Stelle sicher, dass in der Azure App Registration:

- **"Öffentliche Clientflows zulassen"** auf **Ja** steht
- Die Berechtigung `Files.ReadWrite.All` gesetzt ist
- Der Account-Typ **Consumer-Konten** unterstützt

### Port 8080 belegt

```bash
# Prüfen wer den Port nutzt
sudo lsof -i :8080

# Dashboard auf anderem Port starten
uvicorn dashboard.app:app --host 0.0.0.0 --port 9090
```

---

## Deinstallation

```bash
# Timer und Dashboard stoppen
systemctl --user disable --now sortmypdfs.timer
systemctl --user disable --now sortmypdfs-dashboard.service

# Service-Dateien entfernen
rm ~/.config/systemd/user/sortmypdfs.*
rm ~/.config/systemd/user/sortmypdfs-dashboard.service
rm ~/.config/sortmypdfs-dashboard.env
systemctl --user daemon-reload

# Repo entfernen
rm -rf ~/SortmyPDFs

# System-Pakete entfernen (optional)
sudo apt remove --purge -y poppler-utils tesseract-ocr tesseract-ocr-deu
sudo apt autoremove -y
```
