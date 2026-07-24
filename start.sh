#!/usr/bin/env bash
set -o errexit

python manage.py migrate --no-input
python -m gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000}
