#!/usr/bin/env bash
# Build script executed by @vercel/static-build during Vercel deployments.
set -o errexit

# Install Python dependencies in an isolated environment because Vercel's system
# Python is externally managed and rejects direct pip installs.
BUILD_VENV=".vercel-build-venv"
python3 -m venv "$BUILD_VENV"
PYTHON_BIN="$BUILD_VENV/bin/python"
"$PYTHON_BIN" -m pip install -r requirements.txt
"$PYTHON_BIN" manage.py collectstatic --noinput --clear

# Stage collected static files under staticfiles_build/static/ to match the /static/* route.
mkdir -p staticfiles_build
rm -rf staticfiles_build/static
cp -r staticfiles staticfiles_build/static

# Optionally apply database migrations during the build when opted in via environment variable.
if [ "${RUN_MIGRATIONS_ON_BUILD:-false}" = "true" ]; then
    "$PYTHON_BIN" manage.py migrate --noinput
fi
