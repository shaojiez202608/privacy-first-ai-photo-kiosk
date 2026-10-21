#!/usr/bin/env bash
# Canary for the secret scanner: plant a freshly generated fake AWS access key in a throw-away
# directory and confirm that gitleaks (same rules as the history scan) reports it. The fake key only
# exists in a temp directory and is never written into this repository. Exits 1 if it is missed.
set -uo pipefail
TMP=$(mktemp -d)
KEY="AKIA$(LC_ALL=C tr -dc 'A-Z2-7' < /dev/urandom | head -c 16)"
SECRET="$(LC_ALL=C tr -dc 'A-Za-z0-9/+' < /dev/urandom | head -c 40)"
printf 'aws_access_key_id = %s\naws_secret_access_key = %s\n' "$KEY" "$SECRET" > "$TMP/creds.ini"
CONFIG=()
[ -f .gitleaks.toml ] && CONFIG=(--config .gitleaks.toml)
if gitleaks dir "$TMP" ${CONFIG[@]+"${CONFIG[@]}"} --no-banner --redact >/dev/null 2>&1; then
  echo "secret canary NOT detected: the scanner configuration is broken" >&2
  rm "$TMP/creds.ini"; rmdir "$TMP"
  exit 1
fi
rm "$TMP/creds.ini"; rmdir "$TMP"
echo "secret canary detected by gitleaks (expected)"
