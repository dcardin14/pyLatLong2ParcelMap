
#!/usr/bin/env bash
set -euo pipefail

mkdir -p "$PWD/bin"

curl -fL --retry 2 \
  "https://sourceforge.net/projects/exiftool/files/Image-ExifTool-13.60.tar.gz/download" \
  -o /tmp/exiftool.tar.gz

mkdir -p "$PWD/exiftool-package"

tar -xzf /tmp/exiftool.tar.gz \
  -C "$PWD/exiftool-package" \
  --strip-components=1

ln -sf ../exiftool-package/exiftool "$PWD/bin/exiftool"

"$PWD/bin/exiftool" -ver
