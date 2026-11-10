#!/bin/sh
set -eu
exec /usr/bin/tini -- /usr/bin/python3 /opt/browser-runtime/runtime.py "$@"
