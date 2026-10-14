#!/bin/sh
set -eu
export TERM=dumb DEBIAN_FRONTEND=noninteractive LC_ALL=C
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
mkdir -p /var/lib/bibit-intent
chmod 0755 /var/lib/bibit-intent
exec >>/var/log/bibit-system-update.log 2>&1
exec /usr/bin/python3 /opt/bibit-intent/deploy/bibit-system-update.py "$@"
