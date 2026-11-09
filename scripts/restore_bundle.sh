#!/usr/bin/env bash
# Recreate the fork branch from the small incremental delivery bundle.
set -euo pipefail
if [[ $# -ne 2 ]]; then
  printf '%s\n' 'Usage: restore_bundle.sh /absolute/path/rover-web.bundle NEW_DIRECTORY'
  exit 2
fi
BUNDLE_PATH=$(realpath -- "$1")
DESTINATION=$2
if [[ -e "$DESTINATION" ]]; then
  printf '%s\n' 'Destination already exists. Use a new directory to preserve the existing robot installation.'
  exit 1
fi
git clone https://github.com/Freenove/Freenove_Tank_Robot_Kit_for_Raspberry_Pi.git "$DESTINATION"
git -C "$DESTINATION" remote rename origin upstream
git -C "$DESTINATION" bundle verify "$BUNDLE_PATH"
git -C "$DESTINATION" fetch "$BUNDLE_PATH" rover-web:rover-web
git -C "$DESTINATION" switch rover-web
printf '%s\n' "Restored rover-web in $DESTINATION."
