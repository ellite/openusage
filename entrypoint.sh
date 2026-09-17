#!/bin/sh
set -e

PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Timezone
if [ -n "$TZ" ]; then
    ln -snf /usr/share/zoneinfo/"$TZ" /etc/localtime
    echo "$TZ" > /etc/timezone
fi

# Create group/user for the requested PGID/PUID (ignore errors if already exist)
groupadd -g "$PGID" openusage 2>/dev/null || true
useradd -u "$PUID" -g "$PGID" -M -s /bin/false openusage 2>/dev/null || true

# Fix ownership so the openusage user can write to the data directory
chown -R "$PUID:$PGID" /app/data

# Allow the unprivileged user to write to Docker's stdout/stderr
chmod o+w /dev/stdout /dev/stderr

exec gosu openusage uvicorn main:app --host 0.0.0.0 --port 8000
