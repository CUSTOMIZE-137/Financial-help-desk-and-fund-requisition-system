#!/usr/bin/env bash
# Build script executed by @vercel/static-build during Vercel deployments.
set -o errexit

# Install Python dependencies and collect static assets into STATIC_ROOT (staticfiles/).
python3 -m pip install -r requirements.txt
python3 manage.py collectstatic --noinput --clear

# Stage collected static files under staticfiles_build/static/ to match the /static/* route.
mkdir -p staticfiles_build
rm -rf staticfiles_build/static
cp -r staticfiles staticfiles_build/static

# Optionally apply database migrations during the build when opted in via environment variable.
if [ "${RUN_MIGRATIONS_ON_BUILD:-false}" = "true" ]; then
    python3 manage.py migrate --noinput
fi
