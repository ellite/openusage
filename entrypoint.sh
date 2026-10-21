#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}
FORWARDED_ALLOW_IPS=${FORWARDED_ALLOW_IPS:-*}

# Timezone
if [ -n "$TZ" ]; then
    # Not fatal: /etc/localtime may be a mounted file (k8tz, -v /etc/localtime:...) that busybox ln cannot replace
    ln -snf /usr/share/zoneinfo/"$TZ" /etc/localtime 2>/dev/null || echo "Could not set /etc/localtime, keeping the existing one"
    echo "$TZ" > /etc/timezone 2>/dev/null || true
fi

# Fix ownership so the openusage user can write to the data directory
chown -R "$PUID:$PGID" /app/data

# Allow the unprivileged user to write to Docker's stdout/stderr
chmod o+w /dev/stdout /dev/stderr

exec su-exec "$PUID:$PGID" uvicorn main:app --host 0.0.0.0 --port 8000 \
    --proxy-headers --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
