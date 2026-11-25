#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "Usage: $0 MAC_LAN_IP [PYTHON_EXECUTABLE]" >&2
    exit 2
fi
address="$1"
python="${2:-$(command -v python3)}"
src="$root/apps/mac-host"
out="$root/dist"
mkdir -p "$out"
stage="$(mktemp -d "$out/.mac-build.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
app="$stage/Remote Mac Host.app"
bin="$app/Contents/MacOS"
resources="$app/Contents/Resources"
mkdir -p "$bin" "$resources"
"$python" - "$address" "$resources/host-config.json" <<'PY'
import ipaddress
import json
from pathlib import Path
import ssl
import sys

address = ipaddress.IPv4Address(sys.argv[1])
if not address.is_private or address.is_unspecified or address.is_loopback:
    sys.exit("Supply the Mac's private LAN IPv4 address.")
if sys.version_info < (3, 10) or not ssl.HAS_TLSv1_3:
    sys.exit("Python 3.10+ with TLS 1.3 support is required.")
Path(sys.argv[2]).write_text(json.dumps({"address": str(address), "python": sys.executable}))
PY
cp "$src/Info.plist" "$app/Contents/"
cp "$src/stream.py" "$root/packages/protocol/remote_mac_protocol.py" "$resources/"
if [[ -f "$root/assets/branding/RemoteMac.icns" ]]; then
    cp "$root/assets/branding/RemoteMac.icns" "$resources/"
    /usr/libexec/PlistBuddy -c 'Add :CFBundleIconFile string RemoteMac.icns' "$app/Contents/Info.plist"
fi
xcrun clang -fobjc-arc -c "$src/VirtualDisplay.m" -o "$stage/virtual.o"
xcrun swiftc -O "$src/MacHost.swift" -o "$bin/RemoteMacHost"
xcrun swiftc -O -parse-as-library "$src/Capture.swift" -o "$bin/capture"
xcrun swiftc -O -import-objc-header "$src/VirtualDisplay.h" "$src/Control.swift" "$stage/virtual.o" -o "$bin/control"
codesign --force --deep --sign "${REMOTE_MAC_SIGNING_IDENTITY:--}" "$app"
codesign --verify --deep --strict "$app"
ditto -c -k --keepParent "$app" "$stage/Remote-Mac-Mac-Host.zip"
mv -f "$stage/Remote-Mac-Mac-Host.zip" "$out/"
printf '%s\n' "$out/Remote-Mac-Mac-Host.zip"
