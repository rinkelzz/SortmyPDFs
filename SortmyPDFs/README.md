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

3) Sortieren (erst Dry-Run, dann Apply):
- `python sort_and_move.py`
- `python sort_and_move.py --apply`
