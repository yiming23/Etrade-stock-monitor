#!/bin/bash
# restart.sh — rebuild and restart Stock Monitor after a code update
# Usage: ./restart.sh

set -e  # stop on any error

PLIST="$HOME/Library/LaunchAgents/com.stockmonitor.plist"
PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$PROJ_DIR/.venv/bin/python"

echo "📦 Stock Monitor — restart script"
echo "   Project: $PROJ_DIR"
echo ""

# ── 1. Check venv exists ────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
  echo "❌ .venv/bin/python not found."
  echo "   Run: python3.11 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
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

# ── 5. Update plist in LaunchAgents ─────────────────────────────────────────
echo "📋 Updating LaunchAgent plist..."
cp "$PROJ_DIR/com.stockmonitor.plist" "$PLIST"
echo "   ✅ Plist copied to ~/Library/LaunchAgents/"

# ── 6. Reload launchd ────────────────────────────────────────────────────────
echo "🔄 Reloading launchd..."
launchctl unload "$PLIST" 2>/dev/null || true
sleep 1
launchctl load "$PLIST"
sleep 2

# ── 7. Verify running ────────────────────────────────────────────────────────
STATUS=$(launchctl list | grep stockmonitor || true)
if [ -z "$STATUS" ]; then
  echo "❌ Process did not start. Check logs:"
  echo "   cat $PROJ_DIR/logs/launchd_stderr.log"
  exit 1
fi

PID=$(echo "$STATUS" | awk '{print $1}')
if [ "$PID" = "-" ]; then
  EXIT_CODE=$(echo "$STATUS" | awk '{print $2}')
  echo "❌ Process exited immediately (exit code: $EXIT_CODE). Check logs:"
  echo "   cat $PROJ_DIR/logs/launchd_stderr.log"
  exit 1
fi

echo ""
echo "✅ Stock Monitor running (PID $PID)"
echo ""
echo "   Logs:  tail -f $PROJ_DIR/logs/launchd_stdout.log"
echo "   Stop:  launchctl unload $PLIST"
echo ""
