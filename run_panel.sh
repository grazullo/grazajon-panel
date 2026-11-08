#!/bin/bash
# Grazajon Host Panel Runner
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
exec python3 panel.py
