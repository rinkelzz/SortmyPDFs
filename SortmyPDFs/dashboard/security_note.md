# Dashboard security note

If you bind the dashboard to your LAN (`--host 0.0.0.0`), **enable Basic Auth** at minimum.

Set env vars:
- `SORTMYPDFS_DASH_USER`
- `SORTMYPDFS_DASH_PASS`

Buttons/endpoints are only intended for private networks and should always be protected.
