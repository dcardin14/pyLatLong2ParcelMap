#!/usr/bin/env bash
set -euo pipefail

EXIFTOOL_VERSION="13.40"

mkdir -p "$HOME/.local/bin"

curl -fsSL \
  "https://exiftool.org/Image-ExifTool-${EXIFTOOL_VERSION}.tar.gz" \
  -o /tmp/exiftool.tar.gz

mkdir -p /tmp/exiftool-src

tar -xzf /tmp/exiftool.tar.gz \
  -C /tmp/exiftool-src \
  --strip-components=1

cp -R /tmp/exiftool-src/. "$HOME/.local/bin/exiftool-lib/"
ln -sf "$HOME/.local/bin/exiftool-lib/exiftool" "$HOME/.local/bin/exiftool"

"$HOME/.local/bin/exiftool" -ver