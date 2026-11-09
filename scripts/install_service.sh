#!/usr/bin/env bash
# Registers the service but deliberately does not enable/start it.
set -euo pipefail
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $EUID -ne 0 ]]; then
  printf '%s\n' 'Run this script with sudo after installing the Python environment.'
  exit 1
fi
if [[ "$PROJECT_ROOT" == *[[:space:]]* ]] || [[ "$PROJECT_ROOT" == *%* ]]; then
  printf '%s\n' 'Use a deployment path without whitespace or percent signs, such as /opt/freenove-rover.'
  exit 1
fi
if [[ ! -x "$PROJECT_ROOT/.venv/bin/python" ]]; then
  printf '%s\n' 'Install the application environment first.'
  exit 1
fi
install -d -m 0750 /etc/freenove-rover
if [[ ! -e /etc/freenove-rover/config.json ]]; then
  "$PROJECT_ROOT/.venv/bin/python" -m rover_web --print-default-config > /etc/freenove-rover/config.json
  chmod 0640 /etc/freenove-rover/config.json
fi
if [[ ! -e /etc/freenove-rover/access.token ]]; then
  python3 - <<'PY'
import os, secrets
path = '/etc/freenove-rover/access.token'
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as handle:
    handle.write(secrets.token_urlsafe(32) + '\n')
PY
fi
"$PROJECT_ROOT/.venv/bin/python" -m rover_web --config /etc/freenove-rover/config.json --check-config
if [[ -e /etc/systemd/system/rover-web.service ]]; then
  cp -a /etc/systemd/system/rover-web.service "/etc/systemd/system/rover-web.service.backup-$(date +%Y%m%d-%H%M%S)"
fi
cat > /etc/systemd/system/rover-web.service <<EOF
[Unit]
Description=Freenove Rover WebRTC Console
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_ROOT
ExecStart=$PROJECT_ROOT/.venv/bin/python -m rover_web --config /etc/freenove-rover/config.json --token-file /etc/freenove-rover/access.token
Restart=on-failure
RestartSec=3
TimeoutStopSec=7
KillMode=control-group
Environment=PYTHONUNBUFFERED=1
NoNewPrivileges=true
PrivateTmp=true
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 /etc/systemd/system/rover-web.service
systemctl daemon-reload
printf '%s\n' 'Service registered; it is not started or enabled.'
printf '%s\n' 'Stop the identified original camera/GPIO server before starting rover-web.'
printf '%s\n' 'The access key is stored at /etc/freenove-rover/access.token (root-readable only).'
