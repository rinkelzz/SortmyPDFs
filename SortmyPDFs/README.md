# SortmyPDFs

Ziel: Eingescannte PDFs aus OneDrive (Graph API) automatisch in Ordner einsortieren und umbenennen.

## Regeln (aktuell)
- Inbox-Ordner (OneDrive Root): `vomDrucker`
- Sortierung: `/SortmyPDFs/<Empfaenger>/<Firma>/`
  - Empfaenger: `Chantal` | `Tim` | `Sonstige`
  - Firma: z.B. `HDI`
- Dateiname: `YYYY-MM-DD_<Firma>_<Kurztyp>.pdf`
  - Datum = Datum im Brief; falls nicht gefunden: OneDrive `createdDateTime`

## OneDrive Auth (Personal)
Wir nutzen Microsoft Graph `device code flow`.

Du brauchst dafür eine **App Registration** (Client ID) die **Consumer Accounts** unterstützt.

Benötigte Scopes (mindestens):
- `Files.ReadWrite.All`

Hinweis: `offline_access`/`openid`/`profile` sind in MSAL (device code flow) reserviert und werden nicht als normale Scopes übergeben.

## Nächste Schritte
1) Auth:
- `source .venv/bin/activate`
- `python auth_device_code.py`

2) Inbox anzeigen:
- `python graph_list_inbox.py`

3) (Optional) PDFs aus einem IMAP-Postfach einsammeln (Anhänge) und in den OneDrive-Inbox-Ordner legen:
- `.env` befüllen (siehe `.env.example`, IMAP_* Variablen)
- `python imap_ingest.py` (nur UNSEEN)
- `python imap_ingest.py --delete` (löscht Mails nach erfolgreichem Upload)
- `python imap_ingest.py --all --delete` (alle Mails, dann löschen)

4) Sortieren (erst Dry-Run, dann Apply):
- `python sort_and_move.py`
- `python sort_and_move.py --apply`

## IMAP Setup Notes (E-Mail-Archiv-Postfach)
- Der IMAP-Ingest lädt PDF-Anhänge aus einem separaten Archiv-Postfach und legt sie in `vomDrucker/` ab.
- Danach sortiert `sort_and_move.py` wie gewohnt nach `/SortmyPDFs/<Empfaenger>/<Firma>/`.

### Konfiguration (`.env`)
Benötigte Variablen:
- `IMAP_HOST`
- `IMAP_PORT` (typisch 993)
- `IMAP_USER`
- `IMAP_PASSWORD`
- `IMAP_FOLDER` (typisch `INBOX`)

### Troubleshooting
- **SSL Hostname mismatch:** Wenn `ssl.SSLCertVerificationError: Hostname mismatch` kommt, stimmt `IMAP_HOST` nicht zum Zertifikat (SAN/CN) auf Port 993.
- **Delete Flag:** Das Script nutzt das System-Flag `\Deleted` (ein Backslash). Manche Server akzeptieren keine abweichenden Schreibweisen.
