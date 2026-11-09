#!/usr/bin/env bash
# Install only this application. Does not change camera overlays, disable the
# original application, start a service, move actuators, or reboot the rover.
set -euo pipefail
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_ROOT"
SYSTEM_PYTHON=/usr/bin/python3
PI_MODEL=''
if [[ -r /proc/device-tree/model ]]; then
  PI_MODEL=$(tr -d '\0' < /proc/device-tree/model)
fi
if [[ "$PI_MODEL" != *'Raspberry Pi 4'* ]]; then
  printf '%s\n' 'This installer targets Raspberry Pi 4. For desktop demo use: pip install -e ".[desktop,test]"'
  exit 1
fi
"$SYSTEM_PYTHON" -c 'import sys; assert sys.version_info >= (3, 11), "Use Raspberry Pi OS Bookworm/Trixie (Python 3.11+)"'
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip python3-picamera2 python3-opencv python3-numpy python3-gpiozero python3-lgpio
# Apt's camera/OpenCV modules belong to the OS interpreter, not whichever
# custom Python or active virtual environment appears first on PATH.
"$SYSTEM_PYTHON" -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[hardware,test]'
.venv/bin/python -m rover_web --check-config
.venv/bin/python -m pytest -q
printf '%s\n' 'Installed. Existing camera configuration and Freenove application are unchanged.'
printf '%s\n' 'Next: read docs/DEPLOYMENT.md and commission the local configuration.'
