#!/bin/sh
# Ensure the SQLite volume is writable by the unprivileged user, then drop root.
# Hosting platforms (Fly, Railway, Render) mount volumes root-owned.
set -e
DATA_DIR="${DATA_DIR:-/data}"
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R app:app "$DATA_DIR"
    exec setpriv --reuid=app --regid=app --init-groups "$@"
fi
exec "$@"
