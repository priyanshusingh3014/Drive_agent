#!/usr/bin/env bash
set -o errexit

for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if python manage.py migrate --no-input; then
        break
    fi

    if [ "$attempt" = "10" ]; then
        echo "Database migrations failed after 10 attempts."
        exit 1
    fi

    echo "Database is not ready yet. Retrying migrations in 3 seconds..."
    sleep 3
done

exec python -m gunicorn config.wsgi:application --bind 0.0.0.0:${PORT:-8000}
