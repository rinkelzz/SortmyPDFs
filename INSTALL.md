# Installation – SortmyPDFs

Dieser Guide erklärt die komplette Einrichtung von SortmyPDFs auf einem Linux-Server.
Kein manuelles SSH-Gefummel nötig – der mitgelieferte Installer erledigt alles automatisch.

---

## Voraussetzungen

| Was | Details |
|-----|---------|
| **Betriebssystem** | Linux (Ubuntu 20.04+ / Debian 11+ empfohlen) |
| **Python** | 3.10 oder neuer |
| **sudo-Rechte** | nur für apt-Pakete in Schritt 1 |
| **Microsoft-Konto** | OneDrive Personal (Consumer Account) |
| **Azure App Registration** | Client-ID für den Device Code Flow (kostenlos, einmalig) |

---

## Schritt 0 – Azure App Registration erstellen

> Nur einmalig nötig. Dauert ca. 2 Minuten.

1. Öffne [portal.azure.com](https://portal.azure.com) → **Microsoft Entra ID** → **App-Registrierungen** → **Neue Registrierung**
2. Name: z.B. `SortmyPDFs`
3. Kontotypen: **Persönliche Microsoft-Konten** (Consumer)
4. Umleitungs-URI: leer lassen
5. Registrieren → die angezeigte **Anwendungs-ID (Client-ID)** notieren → sie wird während der Installation abgefragt
6. Unter **Authentifizierung** → **Erweiterte Einstellungen** → **Öffentliche Clientflows aktivieren: Ja**

---

## Schritt 1 – Repo klonen

```bash
git clone https://github.com/rinkelzz/SortmyPDFs.git
cd SortmyPDFs
```

---

## Schritt 2 – Installer starten

```bash
chmod +x install.sh
./install.sh
```

Der Installer führt dich interaktiv durch **5 Schritte**:

### 1 / 5 – System-Abhängigkeiten

Prüft und installiert (falls nötig) per `sudo apt`:

- `poppler-utils` – rendert PDF-Seiten als PNG für OCR
- `tesseract-ocr` + `tesseract-ocr-deu` – deutsche OCR-Engine
- `python3`, `python3-venv`, `python3-pip`, `git`

### 2 / 5 – Python-venv & Pakete

Erstellt automatisch ein venv unter `SortmyPDFs/.venv/` und installiert alle pip-Abhängigkeiten (Core + Dashboard).

### 3 / 5 – Konfiguration (.env)

Fragt interaktiv nach:

| Eingabe | Bedeutung | Standard |
|---------|-----------|---------|
| **Graph Client-ID** | Die App-ID aus Schritt 0 | – |
| Tenant | `consumers` für private OneDrive-Konten | `consumers` |
| OneDrive Eingangsordner | Wo dein Scanner ablegt | `vomDrucker` |
| OneDrive Zielordner | Wurzelordner für sortierte Dokumente | `SortmyPDFs` |
| IMAP-Server *(optional)* | Nur wenn du PDFs per E-Mail sammelst | leer |
| IMAP-Port | TLS-Port | `993` |
| IMAP-Benutzer / Passwort | E-Mail-Zugangsdaten | leer |

Die fertige `.env` wird mit `chmod 600` gesichert (nur für deinen Benutzer lesbar).

### 4 / 5 – systemd --user Units

Richtet den automatischen Stundentakt ein:

- **`sortmypdfs.service`** – führt `run_hourly.sh` aus (IMAP-Ingest → Sortieren)
- **`sortmypdfs.timer`** – stündlicher Trigger (zufällige Verzögerung ±2 min)
- **`sortmypdfs-dashboard.service`** *(optional)* – startet das Web-Dashboard im Heimnetz

Für das Dashboard wird zusätzlich abgefragt:

| Eingabe | Bedeutung | Standard |
|---------|-----------|---------|
| Benutzername | Basic-Auth-Login | `admin` |
| Passwort | Basic-Auth-Passwort | – |
| Port | Dashboard-Port | `8080` |
| Live-Inbox | OneDrive-Inbox live anzeigen? | Nein |

Die Zugangsdaten werden in `~/.config/sortmypdfs-dashboard.env` gespeichert (`chmod 600`).

### 5 / 5 – OneDrive-Anmeldung

Startet den einmaligen **Device Code Flow**:

```
To sign in, use a web browser to open the page https://microsoft.com/devicelogin
and enter the code XXXXXXXX to authenticate.
```

1. Link im Browser öffnen (z.B. auf dem PC)
2. Code eingeben
3. Mit deinem Microsoft-Konto anmelden
4. Fertig – das Token wird lokal gecacht

---

## Installer-Optionen

```bash
./install.sh                  # Vollständige Installation (empfohlen)
./install.sh --no-dashboard   # Dashboard-Service überspringen
./install.sh --no-imap        # IMAP-Felder in .env weglassen
./install.sh --dry-run        # Nur anzeigen, was gemacht würde – nichts schreiben
```

---

## Nach der Installation

### Inbox testen

```bash
SortmyPDFs/.venv/bin/python SortmyPDFs/graph_list_inbox.py
```

Zeigt alle PDFs, die aktuell im Eingangsordner liegen.

### Manuellen Lauf starten

```bash
# Dry-Run – zeigt was passieren würde, verschiebt nichts
SortmyPDFs/.venv/bin/python SortmyPDFs/sort_and_move.py

# Apply – verschiebt und benennt Dateien wirklich um
SortmyPDFs/.venv/bin/python SortmyPDFs/sort_and_move.py --apply
```

### Timer-Status prüfen

```bash
systemctl --user status sortmypdfs.timer
systemctl --user status sortmypdfs.service
```

### Dashboard aufrufen

```bash
systemctl --user status sortmypdfs-dashboard.service
```

URL im Heimnetz:
```
http://<deine-lokale-ip>:8080
```

Lokale IP herausfinden: `hostname -I`

### Logs ansehen

```bash
# Letzte Läufe
ls -lt SortmyPDFs/logs/ | head

# Letzten Log anzeigen
cat "$(ls -t SortmyPDFs/logs/hourly-*.log | head -1)"

# systemd Journal
journalctl --user -u sortmypdfs.service -n 50 --no-pager
```

---

## Konfiguration nachträglich ändern

`.env` bearbeiten:
```bash
nano SortmyPDFs/.env
```

Dashboard-Zugangsdaten ändern:
```bash
nano ~/.config/sortmypdfs-dashboard.env
systemctl --user restart sortmypdfs-dashboard.service
```

OneDrive-Auth erneuern (Token abgelaufen):
```bash
SortmyPDFs/.venv/bin/python SortmyPDFs/auth_device_code.py
```

---

## Deinstallation

```bash
# Timer und Dashboard stoppen und deaktivieren
systemctl --user disable --now sortmypdfs.timer sortmypdfs-dashboard.service

# systemd-Units entfernen
rm -f ~/.config/systemd/user/sortmypdfs.{service,timer}
rm -f ~/.config/systemd/user/sortmypdfs-dashboard.service
systemctl --user daemon-reload

# Dashboard-Config entfernen
rm -f ~/.config/sortmypdfs-dashboard.env

# Repo-Ordner entfernen
cd ..
rm -rf SortmyPDFs
```

---

## Troubleshooting

| Problem | Lösung |
|---------|--------|
| `No module named 'msal'` | venv nicht aktiv oder `pip install -r requirements.txt` vergessen |
| `Missing GRAPH_CLIENT_ID` | `.env` fehlt oder Client-ID leer – `nano SortmyPDFs/.env` |
| `No cached account` | OneDrive-Auth fehlt – `python auth_device_code.py` ausführen |
| OCR liefert schlechte Ergebnisse | `tesseract-ocr-deu` prüfen: `dpkg -s tesseract-ocr-deu` |
| `SSL Hostname mismatch` | `IMAP_HOST` in `.env` stimmt nicht mit dem Zertifikat überein |
| Timer läuft nicht | `systemctl --user enable --now sortmypdfs.timer` erneut ausführen |
| Dashboard nicht erreichbar | Firewall prüfen: `sudo ufw allow 8080` |
