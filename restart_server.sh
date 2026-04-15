#!/bin/bash
# restart_server.sh — rebuild and restart Stock Monitor on Linux server
# Usage: ./restart_server.sh

set -e

SERVICE="stockmonitor"
PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJ_DIR/.venv/bin/python"

echo "📦 Stock Monitor — server restart script"
echo "   Project: $PROJ_DIR"
echo ""

# ── 1. Check venv exists ────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
  echo "❌ .venv not found. Run setup_server.sh first."
  exit 1
fi

# ── 2. Install / update dependencies ────────────────────────────────────────
echo "📥 Installing dependencies..."
"$PYTHON" -m pip install -q -r "$PROJ_DIR/requirements.txt"
echo "   ✅ Dependencies up to date"

# ── 3. Quick syntax check ────────────────────────────────────────────────────
echo "🔍 Checking syntax..."
"$PYTHON" -m py_compile \
  src/utils/config.py \
  src/utils/telegram_bot.py \
  src/etrade/auth.py \
  src/etrade/portfolio.py \
  src/analysis/analyzer.py \
  src/email/sender.py \
  src/main.py
echo "   ✅ No syntax errors"

# ── 4. Ensure logs dir exists ────────────────────────────────────────────────
mkdir -p "$PROJ_DIR/logs"

# ── 5. Restart systemd service ───────────────────────────────────────────────
echo "🔄 Restarting service..."
sudo systemctl restart "$SERVICE"
sleep 2

# ── 6. Verify running ────────────────────────────────────────────────────────
if sudo systemctl is-active --quiet "$SERVICE"; then
  PID=$(sudo systemctl show -p MainPID "$SERVICE" | cut -d= -f2)
  echo ""
  echo "✅ Stock Monitor running (PID $PID)"
  echo ""
  echo "   Logs:  tail -f $PROJ_DIR/logs/service.log"
  echo "   Stop:  sudo systemctl stop $SERVICE"
  echo "   Status: sudo systemctl status $SERVICE"
else
  echo "❌ Service failed to start. Check logs:"
  echo "   sudo journalctl -u $SERVICE -n 50"
  echo "   tail -f $PROJ_DIR/logs/service.log"
  exit 1
fi
