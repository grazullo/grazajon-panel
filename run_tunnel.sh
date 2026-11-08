#!/bin/bash
# Cloudflare Tunnel Runner
CF_CONFIG="$HOME/.cloudflared/config.yml"
if [ -f "$CF_CONFIG" ]; then
    exec cloudflared tunnel --protocol http2 --config "$CF_CONFIG" run
else
    exec cloudflared tunnel --protocol http2 run
fi

