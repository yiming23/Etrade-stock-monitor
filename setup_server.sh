#!/bin/bash
# setup_server.sh — one-time setup for Stock Monitor on a fresh Ubuntu server
# Run this ONCE after SSH-ing into your DigitalOcean droplet
# Usage: bash setup_server.sh

set -e

REPO="https://github.com/yiming23/Etrade-stock-monitor"
PROJ_DIR="/home/ubuntu/Etrade-stock-monitor"
SERVICE_FILE="$PROJ_DIR/stockmonitor.service"
SYSTEMD_FILE="/etc/systemd/system/stockmonitor.service"

echo "🚀 Stock Monitor — Server Setup"
echo ""

# ── 1. System packages ───────────────────────────────────────────────────────
echo "📦 Installing system packages..."
sudo apt-get update -q
sudo apt-get install -y -q python3 python3-venv python3-pip git
echo "   ✅ System packages ready"

# ── 2. Clone repo ────────────────────────────────────────────────────────────
if [ -d "$PROJ_DIR" ]; then
  echo "📂 Repo already exists — pulling latest..."
  cd "$PROJ_DIR" && git pull
else
  echo "📂 Cloning repo..."
  git clone "$REPO" "$PROJ_DIR"
fi
echo "   ✅ Code ready"

# ── 3. Python venv ───────────────────────────────────────────────────────────
echo "🐍 Setting up Python environment..."
cd "$PROJ_DIR"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "   ✅ Python environment ready"

# ── 4. Create logs dir ───────────────────────────────────────────────────────
mkdir -p "$PROJ_DIR/logs"
mkdir -p "$PROJ_DIR/data"

# ── 5. Install systemd service ───────────────────────────────────────────────
echo "⚙️  Installing systemd service..."
sudo cp "$SERVICE_FILE" "$SYSTEMD_FILE"
sudo systemctl daemon-reload
sudo systemctl enable stockmonitor
echo "   ✅ Service installed and enabled"

# ── 6. Check for credentials ────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "⚠️  MANUAL STEP REQUIRED — Copy credentials"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Run these commands FROM YOUR MAC:"
echo ""
echo "  scp .env ubuntu@SERVER_IP:$PROJ_DIR/"
echo "  scp credentials.json ubuntu@SERVER_IP:$PROJ_DIR/"
echo "  scp gmail_token.json ubuntu@SERVER_IP:$PROJ_DIR/"
echo ""
echo "Then come back here and run:"
echo "  sudo systemctl start stockmonitor"
echo "  tail -f $PROJ_DIR/logs/service.log"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
