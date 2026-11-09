#!/bin/sh
set -eu
cd "$(dirname "$0")"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
swift render_icon.swift icon.svg "$work/icon-1024.png"
mkdir "$work/ReelsManager.iconset"
for size in 16 32 128 256 512; do
    sips -z $size $size "$work/icon-1024.png" --out "$work/ReelsManager.iconset/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double "$work/icon-1024.png" --out "$work/ReelsManager.iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$work/ReelsManager.iconset" -o ReelsManager.icns
cp "$work/icon-1024.png" icon-preview.png
echo "Wrote packaging/ReelsManager.icns"
