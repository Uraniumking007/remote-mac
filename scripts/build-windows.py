#!/usr/bin/env python3
"""Build the portable Windows x64 viewer using MinGW and pip."""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlretrieve
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "apps" / "windows-viewer"
PYTHON_VERSION = "3.13.11"
PYTHON_ARCHIVE_SHA256 = "1ec066fb61ba5e8c73e29e048cd07c26850f74585e3a116005135b31b8004890"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", type=ipaddress.IPv4Address)
    parser.add_argument("fingerprint", help="SHA-256 fingerprint from setup-mac-lan.sh")
    args = parser.parse_args()
    try:
        fingerprint = bytes.fromhex(args.fingerprint.replace(":", ""))
    except ValueError:
        parser.error("Enter a SHA-256 certificate fingerprint.")
    if len(fingerprint) != 32:
        parser.error("Enter a SHA-256 certificate fingerprint.")
    compiler = shutil.which("x86_64-w64-mingw32-gcc")
    if compiler is None:
        parser.error("Windows x64 MinGW compiler not found: x86_64-w64-mingw32-gcc")

    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".windows-build-", dir=dist) as directory:
        stage = Path(directory)
        bundle = stage / "bundle"
        runtime = bundle / "runtime"
        packages = runtime / "site-packages"
        packages.mkdir(parents=True)
        python_zip = stage / "python.zip"
        urlretrieve(f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip", python_zip)
        with python_zip.open("rb") as archive:
            digest = hashlib.sha256(archive.read()).hexdigest()
        if digest != PYTHON_ARCHIVE_SHA256:
            raise SystemExit("Embedded Python download did not match the expected SHA-256.")
        with ZipFile(python_zip) as archive:
            archive.extractall(runtime)
        (runtime / "python313._pth").write_text("python313.zip\n.\nsite-packages\n..\nimport site\n", encoding="ascii")

        subprocess.run([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
            "--platform", "win_amd64", "--python-version", "3.13", "--implementation", "cp",
            "--abi", "cp313", "--only-binary=:all:", "--no-deps", "--target", str(packages),
            "-r", str(VIEWER / "requirements.txt"),
        ], check=True)
        for name in ("desktop_viewer.py", "viewer.py", "connect.py"):
            shutil.copy(VIEWER / name, bundle / name)
        shutil.copy(ROOT / "packages/protocol/remote_mac_protocol.py", bundle)
        (bundle / "connection.json").write_text(
            json.dumps({"address": str(args.address), "fingerprint": fingerprint.hex()}), encoding="ascii"
        )
        (bundle / "Replace pairing key.cmd").write_bytes(
            b'@echo off\r\ncd /d "%~dp0"\r\nruntime\\python.exe connect.py --replace-key\r\nif errorlevel 1 pause\r\n'
        )
        command = [compiler, "-Os", "-Wall", "-Wextra", "-municode", str(VIEWER / "WindowsLauncher.c")]
        icon = ROOT / "assets/branding/RemoteMac.ico"
        if icon.is_file():
            source = stage / "logo.rc"
            shutil.copy(icon, stage / "RemoteMac.ico")
            source.write_text('1 ICON "RemoteMac.ico"\n', encoding="ascii")
            compiled = stage / "logo.o"
            subprocess.run(["x86_64-w64-mingw32-windres", source.name, compiled.name], cwd=stage, check=True)
            command.append(str(compiled))
        subprocess.run([*command, "-o", str(bundle / "Remote Mac.exe")], check=True)
        archive = shutil.make_archive(str(stage / "Remote-Mac-Python-Windows-x64"), "zip", bundle)
        output = dist / "Remote-Mac-Python-Windows-x64.zip"
        Path(archive).replace(output)
        print(output)


if __name__ == "__main__":
    main()
