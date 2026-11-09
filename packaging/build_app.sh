#!/bin/sh
set -eu
cd "$(dirname "$0")/.."

uv run pyinstaller \
    --noconfirm \
    --clean \
    --windowed \
    --name "Reels Manager" \
    --icon "$PWD/packaging/ReelsManager.icns" \
    --osx-bundle-identifier com.joshua.reelsmanager \
    --add-data "$PWD/reels/static:reels/static" \
    --collect-submodules uvicorn \
    --distpath dist \
    --workpath build \
    --specpath build \
    app_entry.py

echo "Built dist/Reels Manager.app"
