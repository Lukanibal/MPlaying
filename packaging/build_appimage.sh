#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$(mktemp -d "$ROOT_DIR/.appimage-build.XXXXXX")"
APPDIR="$BUILD_DIR/AppDir"
PYINSTALLER_DIST="$BUILD_DIR/pyinstaller-dist"
PYINSTALLER_WORK="$BUILD_DIR/pyinstaller-work"
BUILD_VENV="${BUILD_VENV:-$ROOT_DIR/.venv-appimage}"
OUTPUT_DIR="$ROOT_DIR/dist"
LINUXDEPLOY="${LINUXDEPLOY:-linuxdeploy}"

if [[ "$(uname -m)" != "x86_64" ]]; then
    printf 'AppImage build currently supports x86_64, not %s\n' "$(uname -m)" >&2
    exit 1
fi

LINUXDEPLOY_COMMAND=()
if command -v "$LINUXDEPLOY" >/dev/null 2>&1; then
    LINUXDEPLOY_COMMAND=("$LINUXDEPLOY")
elif command -v distrobox-host-exec >/dev/null 2>&1 \
    && distrobox-host-exec "$LINUXDEPLOY" --version >/dev/null 2>&1; then
    LINUXDEPLOY_COMMAND=(distrobox-host-exec "$LINUXDEPLOY")
else
    printf 'linuxdeploy is unavailable here and through distrobox-host-exec.\n' >&2
    exit 1
fi

if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
    printf 'Tkinter is missing from this Distrobox. Install it with:\n' >&2
    printf '  sudo dnf install python3-tkinter tk\n' >&2
    exit 1
fi

if ! python3 -c 'import dbus' >/dev/null 2>&1; then
    printf 'dbus-python is missing from this Distrobox Python.\n' >&2
    exit 1
fi

if ! command -v objdump >/dev/null 2>&1; then
    if command -v distrobox-host-exec >/dev/null 2>&1 \
        && distrobox-host-exec /usr/bin/objdump --version >/dev/null 2>&1; then
        export PATH="$ROOT_DIR/packaging/host-tools:$PATH"
    else
        printf 'objdump is required; install binutils inside this Distrobox.\n' >&2
        exit 1
    fi
fi

if ! command -v objcopy >/dev/null 2>&1; then
    if command -v distrobox-host-exec >/dev/null 2>&1 \
        && distrobox-host-exec /usr/bin/objcopy --version >/dev/null 2>&1; then
        export PATH="$ROOT_DIR/packaging/host-tools:$PATH"
    else
        printf 'objcopy is required; install binutils inside this Distrobox.\n' >&2
        exit 1
    fi
fi

if [[ ! -x "$BUILD_VENV/bin/python" ]]; then
    python3 -m venv --system-site-packages "$BUILD_VENV"
fi

BUILD_PYTHON="$BUILD_VENV/bin/python"
"$BUILD_PYTHON" -m pip install -r "$ROOT_DIR/requirements-build.txt"
if ! "$BUILD_PYTHON" -c 'import dbus, PIL, tkinter, yaml'; then
    printf 'The build environment must inherit Distrobox dbus-python and Tcl/Tk.\n' >&2
    printf 'Create %s with --system-site-packages, then retry.\n' "$BUILD_VENV" >&2
    exit 1
fi

"$BUILD_PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --name mplaying \
    --distpath "$PYINSTALLER_DIST" \
    --workpath "$PYINSTALLER_WORK" \
    --specpath "$BUILD_DIR" \
    --collect-all dbus \
    --collect-all yaml \
    --collect-all PIL \
    --hidden-import _dbus_bindings \
    "$ROOT_DIR/now_playing.py"

mkdir -p "$APPDIR/usr/bin" "$OUTPUT_DIR"
install -m 0755 "$PYINSTALLER_DIST/mplaying" "$APPDIR/usr/bin/mplaying"
"$BUILD_PYTHON" "$ROOT_DIR/packaging/create_icon.py" "$BUILD_DIR/mplaying.png"

(
    cd "$OUTPUT_DIR"
    "${LINUXDEPLOY_COMMAND[@]}" \
        --appdir "$APPDIR" \
        --executable "$APPDIR/usr/bin/mplaying" \
        --desktop-file "$ROOT_DIR/packaging/MPlaying.desktop" \
        --icon-file "$BUILD_DIR/mplaying.png" \
        --output appimage
)

if ! find "$OUTPUT_DIR" -maxdepth 1 -type f -name '*.AppImage' -print -quit | grep -q .; then
    printf 'linuxdeploy completed without producing an AppImage in %s\n' "$OUTPUT_DIR" >&2
    exit 1
fi
printf 'AppImage build complete. See %s/*.AppImage\n' "$OUTPUT_DIR"