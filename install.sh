#!/usr/bin/env bash
# =============================================================================
#  SortmyPDFs – Installer
#
#  Führt alle Schritte durch, die sonst manuell über Putty/SSH nötig wären:
#  1. System-Abhängigkeiten (poppler, tesseract)
#  2. Python-venv + pip-Pakete
#  3. .env anlegen und befüllen
#  4. systemd --user Units installieren (Timer + optional Dashboard-Service)
#  5. OneDrive-Auth starten (Device Code Flow)
#
#  Aufruf:
#    chmod +x install.sh
#    ./install.sh
#
#  Optionen:
#    --no-dashboard    Dashboard-Service überspringen
#    --no-imap         IMAP-Felder in .env überspringen
#    --dry-run         Nur anzeigen, was gemacht würde (kein Schreiben)
# =============================================================================
set -euo pipefail

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()      { echo -e "${GREEN}[ OK ]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERR ]${NC}  $*" >&2; }
heading() { echo -e "\n${CYAN}══════════════════════════════════════════${NC}"; echo -e "${CYAN}  $*${NC}"; echo -e "${CYAN}══════════════════════════════════════════${NC}"; }

ask() {
    # ask <Variable> <Prompt> [Default]
    local var="$1" prompt="$2" default="${3:-}"
    if [[ -n "$default" ]]; then
        read -rp "  $prompt [$default]: " _val
        printf -v "$var" '%s' "${_val:-$default}"
    else
        read -rp "  $prompt: " _val
        printf -v "$var" '%s' "$_val"
    fi
}

ask_secret() {
    local var="$1" prompt="$2"
    read -rsp "  $prompt: " _val
    echo
    printf -v "$var" '%s' "$_val"
}

confirm() {
    # confirm <Prompt> [default y/n]
    local prompt="$1" default="${2:-y}"
    local yn_hint="[Y/n]"; [[ "$default" == "n" ]] && yn_hint="[y/N]"
    read -rp "  $prompt $yn_hint: " _c
    _c="${_c:-$default}"
    [[ "$_c" =~ ^[Yy] ]]
}

DRY_RUN=false
SKIP_DASHBOARD=false
SKIP_IMAP=false

for arg in "$@"; do
    case "$arg" in
        --dry-run)      DRY_RUN=true ;;
        --no-dashboard) SKIP_DASHBOARD=true ;;
        --no-imap)      SKIP_IMAP=true ;;
        -h|--help)
            echo "Verwendung: $0 [--dry-run] [--no-dashboard] [--no-imap]"
            exit 0 ;;
    esac
done

run() {
    if $DRY_RUN; then
        echo -e "  ${YELLOW}[DRY-RUN]${NC} $*"
    else
        "$@"
    fi
}

# ---------------------------------------------------------------------------
# Pfade ermitteln
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/SortmyPDFs"
VENV_DIR="$APP_DIR/.venv"
ENV_FILE="$APP_DIR/.env"
SYSTEMD_DIR="$HOME/.config/systemd/user"
DASH_ENV_FILE="$HOME/.config/sortmypdfs-dashboard.env"

# ---------------------------------------------------------------------------
# Intro
# ---------------------------------------------------------------------------
clear
echo -e "${CYAN}"
cat << 'EOF'
  ____             _              _____  ____  ___
 / ___|  ___  _ __| |_ _ __ ___ |  __ \|  _ \|  ___|
 \___ \ / _ \| '__| __| '_ ` _ \| |__) | | | | |_
  ___) | (_) | |  | |_| | | | | |  ___/| |_| |  _|
 |____/ \___/|_|   \__|_| |_| |_|_|    |____/|_|

  Automatischer Installer
EOF
echo -e "${NC}"

if $DRY_RUN; then
    warn "DRY-RUN Modus – es wird nichts wirklich installiert oder geschrieben."
fi

echo "  Installationsverzeichnis: $APP_DIR"
echo ""

# ---------------------------------------------------------------------------
# 1. System-Abhängigkeiten
# ---------------------------------------------------------------------------
heading "1 / 5 – System-Abhängigkeiten"

MISSING_PKGS=()
for pkg in poppler-utils tesseract-ocr tesseract-ocr-deu python3 python3-venv python3-pip git; do
    if ! dpkg -s "$pkg" &>/dev/null 2>&1; then
        MISSING_PKGS+=("$pkg")
    fi
done

if [[ ${#MISSING_PKGS[@]} -eq 0 ]]; then
    ok "Alle System-Pakete bereits installiert."
else
    warn "Fehlende Pakete: ${MISSING_PKGS[*]}"
    if confirm "Jetzt mit sudo apt installieren?"; then
        run sudo apt-get update -qq
        run sudo apt-get install -y "${MISSING_PKGS[@]}"
        ok "Pakete installiert."
    else
        error "Installation abgebrochen. Bitte Pakete manuell installieren."
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# 2. Python venv + Pakete
# ---------------------------------------------------------------------------
heading "2 / 5 – Python venv & Pakete"

if [[ -d "$VENV_DIR" ]]; then
    ok "venv bereits vorhanden: $VENV_DIR"
else
    info "Erstelle venv in $VENV_DIR …"
    run python3 -m venv "$VENV_DIR"
    ok "venv erstellt."
fi

PY="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

info "Installiere Python-Pakete (core) …"
run "$PIP" install --upgrade pip --quiet
run "$PIP" install -r "$APP_DIR/requirements.txt" --quiet
ok "Core-Pakete installiert."

if ! $SKIP_DASHBOARD; then
    info "Installiere Python-Pakete (Dashboard) …"
    run "$PIP" install -r "$APP_DIR/dashboard/requirements.txt" --quiet
    ok "Dashboard-Pakete installiert."
fi

# ---------------------------------------------------------------------------
# 3. .env anlegen und befüllen
# ---------------------------------------------------------------------------
heading "3 / 5 – Konfiguration (.env)"

if [[ -f "$ENV_FILE" ]]; then
    ok ".env existiert bereits: $ENV_FILE"
    if ! confirm "Trotzdem neu befüllen / überschreiben?"; then
        info ".env wird übersprungen."
        ENV_SKIP=true
    fi
fi

ENV_SKIP="${ENV_SKIP:-false}"

if ! $ENV_SKIP; then
    echo ""
    echo "  Bitte gib deine Konfigurationswerte ein."
    echo "  (Leerlassen → Standardwert in Klammern wird genutzt)"
    echo ""

    ask GRAPH_CLIENT_ID  "Microsoft Graph Client-ID (App Registration)"
    ask GRAPH_TENANT     "Tenant (consumers = privates Konto)" "consumers"
    ask GRAPH_SCOPES     "Graph Scopes" "Files.ReadWrite.All"
    ask ONEDRIVE_INBOX   "OneDrive Eingangsordner (Drucker-Scans)" "vomDrucker"
    ask ONEDRIVE_TARGET  "OneDrive Zielordner (root)" "SortmyPDFs"

    if [[ -z "$GRAPH_CLIENT_ID" ]]; then
        warn "GRAPH_CLIENT_ID ist leer! Du kannst sie später in $ENV_FILE eintragen."
    fi

    IMAP_HOST=""; IMAP_PORT="993"; IMAP_USER=""; IMAP_PASSWORD=""; IMAP_FOLDER="INBOX"
    if ! $SKIP_IMAP; then
        echo ""
        echo "  ── IMAP (optional – nur wenn du PDFs per E-Mail bekommst) ──"
        if confirm "IMAP-Einstellungen konfigurieren?"; then
            ask      IMAP_HOST   "IMAP-Server (z.B. imap.gmail.com)"
            ask      IMAP_PORT   "IMAP-Port" "993"
            ask      IMAP_USER   "IMAP-Benutzername / E-Mail-Adresse"
            ask_secret IMAP_PASSWORD "IMAP-Passwort / App-Passwort"
            ask      IMAP_FOLDER "IMAP-Ordner" "INBOX"
        fi
    fi

    info "Schreibe $ENV_FILE …"
    if ! $DRY_RUN; then
        cat > "$ENV_FILE" << ENVEOF
# Microsoft Graph App Registration
GRAPH_TENANT=${GRAPH_TENANT}
GRAPH_CLIENT_ID=${GRAPH_CLIENT_ID}
GRAPH_SCOPES="${GRAPH_SCOPES}"

# OneDrive folders
ONEDRIVE_INBOX=${ONEDRIVE_INBOX}
ONEDRIVE_TARGET_ROOT=${ONEDRIVE_TARGET}

# IMAP (optional)
IMAP_HOST=${IMAP_HOST}
IMAP_PORT=${IMAP_PORT}
IMAP_USER=${IMAP_USER}
IMAP_PASSWORD=${IMAP_PASSWORD}
IMAP_FOLDER=${IMAP_FOLDER}
ENVEOF
        chmod 600 "$ENV_FILE"
    fi
    ok ".env gespeichert."
fi

# ---------------------------------------------------------------------------
# 4. systemd --user Units
# ---------------------------------------------------------------------------
heading "4 / 5 – systemd --user Units"

run mkdir -p "$SYSTEMD_DIR"

# -- Service + Timer (stündlicher Lauf) -------------------------------------
info "Schreibe sortmypdfs.service …"
if ! $DRY_RUN; then
    cat > "$SYSTEMD_DIR/sortmypdfs.service" << UNITEOF
[Unit]
Description=SortmyPDFs hourly ingest+sort (IMAP -> OneDrive -> rename/move)

[Service]
Type=oneshot
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/run_hourly.sh
StandardOutput=journal
StandardError=journal
UNITEOF
fi
ok "sortmypdfs.service geschrieben."

info "Schreibe sortmypdfs.timer …"
if ! $DRY_RUN; then
    cat > "$SYSTEMD_DIR/sortmypdfs.timer" << UNITEOF
[Unit]
Description=Run SortmyPDFs every hour

[Timer]
OnCalendar=hourly
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
UNITEOF
fi
ok "sortmypdfs.timer geschrieben."

# -- Dashboard Service (optional) -------------------------------------------
if ! $SKIP_DASHBOARD; then
    echo ""
    echo "  ── Dashboard ──"

    ask DASH_USER "Dashboard Benutzername" "admin"
    ask_secret DASH_PASS "Dashboard Passwort"
    ask DASH_PORT "Dashboard Port" "8080"

    DASH_LIVE="0"
    confirm "OneDrive-Inbox live im Dashboard anzeigen?" && DASH_LIVE="1" || true

    info "Schreibe $DASH_ENV_FILE …"
    if ! $DRY_RUN; then
        mkdir -p "$(dirname "$DASH_ENV_FILE")"
        cat > "$DASH_ENV_FILE" << DASHENVEOF
SORTMYPDFS_DASH_USER=${DASH_USER}
SORTMYPDFS_DASH_PASS=${DASH_PASS}
SORTMYPDFS_DASH_BUTTONS=1
SORTMYPDFS_DASH_LIVE_INBOX=${DASH_LIVE}
DASHENVEOF
        chmod 600 "$DASH_ENV_FILE"
    fi
    ok "Dashboard-Env gespeichert."

    info "Schreibe sortmypdfs-dashboard.service …"
    if ! $DRY_RUN; then
        cat > "$SYSTEMD_DIR/sortmypdfs-dashboard.service" << UNITEOF
[Unit]
Description=SortmyPDFs Dashboard (LAN)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
EnvironmentFile=${DASH_ENV_FILE}
ExecStart=${VENV_DIR}/bin/uvicorn dashboard.app:app --host 0.0.0.0 --port ${DASH_PORT}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
UNITEOF
    fi
    ok "sortmypdfs-dashboard.service geschrieben."
fi

# -- systemd reload & enable ------------------------------------------------
info "Lade systemd --user Daemon …"
run systemctl --user daemon-reload

info "Aktiviere + starte sortmypdfs.timer …"
run systemctl --user enable --now sortmypdfs.timer
ok "Timer aktiv."

if ! $SKIP_DASHBOARD; then
    info "Aktiviere + starte sortmypdfs-dashboard.service …"
    run systemctl --user enable --now sortmypdfs-dashboard.service
    ok "Dashboard-Service gestartet."
fi

# ---------------------------------------------------------------------------
# 5. OneDrive Auth (Device Code Flow)
# ---------------------------------------------------------------------------
heading "5 / 5 – OneDrive Authentifizierung"

echo ""
echo "  Jetzt wird die einmalige OneDrive-Anmeldung gestartet."
echo "  → Du bekommst einen Code und einen Link."
echo "  → Öffne den Link im Browser, gib den Code ein und melde dich an."
echo ""

if confirm "Auth jetzt starten?"; then
    run "$PY" "$APP_DIR/auth_device_code.py"
    ok "Auth abgeschlossen. Token gespeichert."
else
    warn "Auth übersprungen. Später ausführen:"
    echo "       $PY $APP_DIR/auth_device_code.py"
fi

# ---------------------------------------------------------------------------
# Abschluss
# ---------------------------------------------------------------------------
heading "Installation abgeschlossen"

echo ""
ok "SortmyPDFs wurde eingerichtet!"
echo ""
echo "  Nächste Schritte:"
echo ""
echo "  ▸ Inbox testen:"
echo "      $PY $APP_DIR/graph_list_inbox.py"
echo ""
echo "  ▸ Manueller Lauf (Dry-Run):"
echo "      $PY $APP_DIR/sort_and_move.py"
echo ""
echo "  ▸ Manueller Lauf (wirklich verschieben):"
echo "      $PY $APP_DIR/sort_and_move.py --apply"
echo ""
echo "  ▸ Timer-Status:"
echo "      systemctl --user status sortmypdfs.timer"
echo "      systemctl --user status sortmypdfs.service"

if ! $SKIP_DASHBOARD; then
    echo ""
    LOCAL_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo '<deine-ip>')"
    echo "  ▸ Dashboard: http://${LOCAL_IP}:${DASH_PORT:-8080}"
    echo "      systemctl --user status sortmypdfs-dashboard.service"
fi

echo ""
echo "  ▸ Logs:"
echo "      ls -lt $APP_DIR/logs/ | head"
echo "      journalctl --user -u sortmypdfs.service -n 50 --no-pager"
echo ""
