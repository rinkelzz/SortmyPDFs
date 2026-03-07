#!/usr/bin/env bash
set -euo pipefail

BASE="/home/tim/.openclaw/workspace/SortmyPDFs"
LOG_DIR="$BASE/logs"
TS="$(date -u +'%Y-%m-%dT%H-%M-%SZ')"
LOG_FILE="$LOG_DIR/hourly-$TS.log"

mkdir -p "$LOG_DIR"

{
  echo "== SortmyPDFs hourly run =="
  echo "UTC: $(date -u)"
  echo

  cd "$BASE"
  # Use venv python directly (no interactive shell assumptions)
  PY="$BASE/.venv/bin/python"

  echo "[1/2] IMAP ingest (UNSEEN, delete on success)"
  "$PY" imap_ingest.py --delete
  echo

  echo "[2/2] Sort & move (apply)"
  "$PY" sort_and_move.py --apply
  echo

  echo "Done."
} >"$LOG_FILE" 2>&1

echo "$LOG_FILE"
