#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
  mkdir -p /data
  chown app:app /data
  find /data -mindepth 1 -maxdepth 1 ! -name .tailscale_state -exec chown -R app:app {} +
  exec gosu app "$@"
fi

exec "$@"
