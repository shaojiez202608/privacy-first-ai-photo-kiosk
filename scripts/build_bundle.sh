#!/usr/bin/env bash
# Build the linux/arm64 Lambda code bundle without Docker: wheels are downloaded for the target
# platform with --only-binary, then the project's own packages are copied in (no __pycache__).
# Each build goes to a new directory build/bundle.XXXXXX and build/bundle.current names it; the
# CDK app reads that file. Nothing is deleted here (old build directories stay under the
# git-ignored build/ until you remove them yourself).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p build
OUT=$(mktemp -d build/bundle.XXXXXX)
uv pip install \
  --python-platform aarch64-manylinux_2_28 \
  --python-version 3.14 \
  --target "$OUT" \
  --only-binary=:all: \
  --exclude-newer "7 days" \
  --no-compile \
  --quiet \
  -r src/requirements.txt
rsync -a --exclude '__pycache__' --exclude '*.pyc' src/kiosk src/handlers "$OUT/"
chmod -R u=rwX,go=rX "$OUT"
echo "$OUT" > build/bundle.current
du -sh "$OUT"
