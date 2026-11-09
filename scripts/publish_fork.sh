#!/usr/bin/env bash
# Run on a computer where GitHub CLI is already authenticated as mathisono.
set -euo pipefail
PROJECT_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$PROJECT_ROOT"
UPSTREAM_REPO='Freenove/Freenove_Tank_Robot_Kit_for_Raspberry_Pi'
FORK_NAME='Freenove_Tank_Robot_Kit_for_Raspberry_Pi'
gh auth status --hostname github.com
FORK_OWNER=$(gh api user --jq .login)
if [[ "$FORK_OWNER" != 'mathisono' ]]; then
  printf '%s\n' "Authenticated account is $FORK_OWNER; expected mathisono. No repository was changed."
  exit 1
fi
if gh repo view "$FORK_OWNER/$FORK_NAME" --json name > /dev/null 2>&1; then
  FORK_PARENT=$(gh repo view "$FORK_OWNER/$FORK_NAME" --json parent --jq '.parent.owner.login + "/" + .parent.name')
  if [[ "$FORK_PARENT" != "$UPSTREAM_REPO" ]]; then
    printf '%s\n' 'A repository with that name exists but is not the expected Freenove fork. Stopping.'
    exit 1
  fi
else
  gh repo fork "$UPSTREAM_REPO" --fork-name "$FORK_NAME" --clone=false --remote=false
fi
FORK_URL="https://github.com/$FORK_OWNER/$FORK_NAME.git"
if git remote get-url origin > /dev/null 2>&1; then
  EXISTING_URL=$(git remote get-url origin)
  if [[ "$EXISTING_URL" != "$FORK_URL" ]] && [[ "$EXISTING_URL" != "git@github.com:$FORK_OWNER/$FORK_NAME.git" ]]; then
    printf '%s\n' 'origin already points elsewhere; no remote was overwritten.'
    exit 1
  fi
else
  git remote add origin "$FORK_URL"
fi
git -c credential.helper= -c 'credential.https://github.com.helper=!gh auth git-credential' push --set-upstream origin rover-web
printf '%s\n' "Published $FORK_OWNER/$FORK_NAME branch rover-web."
