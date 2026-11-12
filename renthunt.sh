#!/usr/bin/env bash
# RentHunt launcher.  ./renthunt.sh [start|hunt|tunnel|install-service|uninstall-service|status]
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
PY=.venv/bin/python
PORT="${RENTHUNT_PORT:-8642}"
UNIT="$HOME/.config/systemd/user/renthunt.service"

if [ ! -x "$PY" ]; then
  echo "Setting up the Python environment (first run only)…"
  if command -v uv >/dev/null; then uv venv -q .venv && uv pip install -q --python "$PY" -e .
  else python3 -m venv .venv && "$PY" -m pip install -q -e .; fi
fi

case "${1:-start}" in
  start)
    echo "RentHunt is running at http://127.0.0.1:$PORT  (Ctrl+C to stop)"
    exec "$PY" -m renthunt.app ;;
  hunt)   # one hunt from the command line, without the web app
    exec "$PY" -c 'from renthunt import db, hunt; db.init(); r = hunt.run("cli"); print(hunt.last_run()["log"] if r else "A hunt is already running.")' ;;
  install-service)   # keep RentHunt (and its schedule) running in the background
    mkdir -p "$(dirname "$UNIT")"
    cat > "$UNIT" <<EOF
[Unit]
Description=RentHunt rental listing hunter
After=network-online.target

[Service]
WorkingDirectory=$PWD
ExecStart=$PWD/$PY -m renthunt.app
Environment=RENTHUNT_PORT=$PORT
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now renthunt.service
    echo "Installed. RentHunt now starts automatically: http://127.0.0.1:$PORT"
    echo "Logs: journalctl --user -u renthunt -f" ;;
  uninstall-service)
    systemctl --user disable --now renthunt.service 2>/dev/null || true
    rm -f "$UNIT"; systemctl --user daemon-reload
    echo "Background service removed." ;;
  tunnel)   # share the running app on a public address (visitors are read-only)
    command -v cloudflared >/dev/null || { echo "cloudflared is not installed."; exit 1; }
    echo "Admin password for remote sign-in: $(cat data/admin_password 2>/dev/null || echo '(start the app once first)')"
    exec cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" ;;
  status)
    systemctl --user status renthunt.service --no-pager 2>/dev/null || echo "Background service is not installed." ;;
  *) echo "Usage: $0 [start|hunt|tunnel|install-service|uninstall-service|status]"; exit 1 ;;
esac
