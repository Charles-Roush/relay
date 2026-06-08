#!/bin/bash
# Oracle Cloud setup script for Relay AI Training Coach
# Run as: bash setup.sh
set -e

REPO_URL="https://github.com/YOUR_USERNAME/relay.git"  # update this
APP_DIR="/home/ubuntu/relay"
SERVICE_FILE="$APP_DIR/deploy/relay.service"

echo "==> Updating system packages..."
sudo apt-get update -q
sudo apt-get install -y python3 python3-pip python3-venv git

echo "==> Cloning repo..."
if [ -d "$APP_DIR" ]; then
    cd "$APP_DIR" && git pull
else
    git clone "$REPO_URL" "$APP_DIR"
fi

cd "$APP_DIR"

echo "==> Creating virtual environment..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip -q
./venv/bin/pip install -r requirements.txt -q

echo "==> Setting up persistent data directories..."
mkdir -p logs
# Ensure athlete_profile.md and coach_notes.md exist
touch athlete_profile.md coach_notes.md

echo "==> Checking for .env file..."
if [ ! -f ".env" ]; then
    echo "ERROR: .env file not found. Create it at $APP_DIR/.env with:"
    echo "  GARMIN_EMAIL=your@email.com"
    echo "  GARMIN_PASSWORD=yourpassword"
    echo "  ANTHROPIC_API_KEY=sk-ant-..."
    echo "  TELEGRAM_BOT_TOKEN=..."
    echo "  TELEGRAM_CHAT_ID=..."
    exit 1
fi

echo "==> Installing systemd service..."
sudo cp "$SERVICE_FILE" /etc/systemd/system/relay.service
sudo systemctl daemon-reload
sudo systemctl enable relay
sudo systemctl restart relay

echo ""
echo "==> Done. Bot is running."
echo "    Check status: sudo systemctl status relay"
echo "    View logs:    sudo journalctl -u relay -f"
