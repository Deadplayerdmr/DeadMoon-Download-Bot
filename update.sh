#!/usr/bin/env bash

# Ensure the script is run from the directory where it is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR" || exit 1

# ===========================================
# CUSTOM FORK - Skip git pull to preserve custom modifications
# ===========================================
# This bot has been customized with the following features:
# - Whitelist/Blacklist system (/allow, /block, /blockall, /whitelist)
# - Auto-delete messages and server files
# - YouTube/SoundCloud zip support
# - Improved audio tagging with mutagen
# - Session auto-refresh on expired tokens
#
# To manually update from upstream while preserving changes:
# 1. git stash
# 2. git pull
# 3. git stash pop
# 4. Resolve any conflicts manually
#
# Or check for updates: git fetch && git log HEAD..origin/master --oneline
# ===========================================

echo "=== DeadMoon_Downloader Build ==="
echo "Skipping git pull to preserve custom modifications."
echo "To check for upstream updates: git fetch && git log HEAD..origin/master --oneline"
echo ""

# Check if `token.env` file exists
if [ ! -f token.env ]; then
  echo "token.env file not found"
  exit 1
fi

# Migrate multi-field inline credentials before reading configuration or starting Compose.
if command -v python3 &>/dev/null; then
  python3 -m dl_utils.credential_migrator
fi

# Parse only the flag needed by the build; never execute token.env as shell code.
ENABLE_FLAC_VAL="$(grep -E '^ENABLE_FLAC=' token.env | tail -n 1 | cut -d= -f2- | tr -d "[:space:]\"'")"
if [ "$ENABLE_FLAC_VAL" = "0" ] || [ "${ENABLE_FLAC_VAL,,}" = "false" ] || [ "${ENABLE_FLAC_VAL,,}" = "no" ] || [ "${ENABLE_FLAC_VAL,,}" = "off" ]; then
  export ENABLE_FLAC="0"
  echo "FLAC is disabled"
else
  export ENABLE_FLAC="1"
  echo "FLAC is enabled"
fi

# Check if docker compose is available
if docker compose version &>/dev/null; then
  if command -v python3 &>/dev/null; then
    python3 -m dl_utils.ascii_art
  fi
  docker compose up -d --build
# Fall back to docker-compose if docker compose is not available
elif command -v docker-compose &>/dev/null; then
  if command -v python3 &>/dev/null; then
    python3 -m dl_utils.ascii_art
  fi
  docker-compose up -d --build
else
  echo "Neither docker compose nor docker-compose is available"
  exit 1
fi
