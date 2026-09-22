#!/usr/bin/env bash
# Regenerate the Python bindings in protocols/ from the Wayland protocol XML.
# Re-run this if the wlr-protocols package is updated.
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/pywayland-scanner \
    -i /usr/share/wayland/wayland.xml \
       /usr/share/wlr-protocols/unstable/wlr-output-management-unstable-v1.xml \
    -o protocols
touch protocols/__init__.py
