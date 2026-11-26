#!/usr/bin/env python3
"""Build the portable Windows x64 viewer and its per-user NSIS installer."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.request import urlretrieve
from zipfile import ZipFile
import windows_codec_lgpl

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / "apps" / "windows-viewer"
PYTHON_VERSION = "3.13.11"
PYTHON_ARCHIVE_SHA256 = "1ec066fb61ba5e8c73e29e048cd07c26850f74585e3a116005135b31b8004890"
VIEWER_VERSION = "1.0.0"


def _launcher_toolchain():
    if sys.platform == "win32" and shutil.which("cl") and shutil.which("link"):
        return "msvc"
    if shutil.which("x86_64-w64-mingw32-gcc"):
        return "mingw"
    return None


def _compile_launcher(stage, output):
    toolchain = _launcher_toolchain()
    if toolchain is None:
        raise SystemExit("No launcher compiler. Need MSVC cl.exe or x86_64-w64-mingw32-gcc.")
    icon = ROOT / "assets/branding/RemoteMac.ico"
    if toolchain == "msvc":
        built = stage / "RemoteMac.exe"
        command = [
            "cl",
            "/nologo",
            "/O1",
            "/W3",
            f"/Fo{stage / 'RemoteMac.obj'}",
            str(VIEWER / "WindowsLauncher.c"),
            f"/Fe{built}",
        ]
        if icon.is_file():
            if shutil.which("rc") is None:
                raise SystemExit("rc.exe is not on PATH. The MSVC launcher needs the Windows SDK resource compiler.")
            shutil.copy(icon, stage / "RemoteMac.ico")
            resource = stage / "logo.rc"
            resource.write_text('1 ICON "RemoteMac.ico"\n', encoding="ascii")
            compiled = stage / "logo.res"
            subprocess.run(["rc", "/nologo", f"/fo{compiled}", str(resource)], cwd=stage, check=True)
            command.append(str(compiled))
        command.extend(["/link", "/SUBSYSTEM:CONSOLE", "/ENTRY:wmainCRTStartup"])
        subprocess.run(command, cwd=stage, check=True)
        built.replace(output)
        return
    command = ["x86_64-w64-mingw32-gcc", "-Os", "-Wall", "-Wextra", "-municode", str(VIEWER / "WindowsLauncher.c")]
    if icon.is_file():
        source = stage / "logo.rc"
        shutil.copy(icon, stage / "RemoteMac.ico")
        source.write_text('1 ICON "RemoteMac.ico"\n', encoding="ascii")
        compiled = stage / "logo.o"
        subprocess.run(["x86_64-w64-mingw32-windres", source.name, compiled.name], cwd=stage, check=True)
        command.append(str(compiled))
    subprocess.run([*command, "-o", str(output)], check=True)


def build_installer(bundle, dist):
    windows_codec_lgpl.assert_release_bundle(bundle)
    compiler = shutil.which("makensis")
    if compiler is None:
        message = "makensis not found; portable ZIP is ready. Install NSIS to build the Setup.exe."
        if os.environ.get("REMOTE_MAC_REQUIRE_INSTALLER") == "1":
            raise SystemExit(message)
        print(message, file=sys.stderr)
        return None
    parts = VIEWER_VERSION.split(".")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        raise SystemExit("VIEWER_VERSION must be three numeric parts, such as 1.0.0.")
    output = dist / f"Remote-Mac-Windows-x64-{VIEWER_VERSION}-Setup.exe"
    command = [
        compiler,
        f"-DAPP_VERSION={VIEWER_VERSION}",
        f"-DSTAGE={bundle.as_posix()}",
        f"-DOUTFILE={output.as_posix()}",
    ]
    icon = ROOT / "assets/branding/RemoteMac.ico"
    if icon.is_file():
        command.append(f"-DAPP_ICON={icon.as_posix()}")
    command.append(str(ROOT / "scripts" / "windows-installer.nsi"))
    subprocess.run(command, check=True)
    print(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address", nargs="?", type=ipaddress.IPv4Address)
    parser.add_argument(
        "fingerprint", nargs="?",
        help="SHA-256 fingerprint from the Mac. Omit both arguments for a generic build.",
    )
    args = parser.parse_args()
    if (args.address is None) != (args.fingerprint is None):
        parser.error("Supply a LAN address and certificate fingerprint together, or neither for a generic build.")
    fingerprint = None
    if args.fingerprint is not None:
        try:
            fingerprint = bytes.fromhex(args.fingerprint.replace(":", ""))
        except ValueError:
            parser.error("Enter a SHA-256 certificate fingerprint.")
        if len(fingerprint) != 32:
            parser.error("Enter a SHA-256 certificate fingerprint.")
    if not _launcher_toolchain():
        parser.error(
            "Need cl.exe and link.exe on Windows, or x86_64-w64-mingw32-gcc, to build Remote Mac.exe."
        )

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

        requirements = stage / "requirements-without-av.txt"
        requirements.write_text(windows_codec_lgpl.filtered_requirements(VIEWER / "requirements.txt"), encoding="utf-8")
        subprocess.run([
            sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-compile",
            "--platform", "win_amd64", "--python-version", "3.13", "--implementation", "cp",
            "--abi", "cp313", "--only-binary=:all:", "--no-deps", "--target", str(packages),
            "-r", str(requirements),
        ], check=True)
        for name in ("desktop_viewer.py", "viewer.py", "connect.py"):
            shutil.copy(VIEWER / name, bundle / name)
        shutil.copy(ROOT / "packages/protocol/remote_mac_protocol.py", bundle)
        if fingerprint is not None:
            (bundle / "connection.json").write_text(
                json.dumps({"address": str(args.address), "fingerprint": fingerprint.hex()}), encoding="ascii"
            )
        (bundle / "Replace pairing key.cmd").write_bytes(
            b'@echo off\r\ncd /d "%~dp0"\r\nruntime\\python.exe connect.py --replace-key\r\nif errorlevel 1 pause\r\n'
        )
        _compile_launcher(stage, bundle / "Remote Mac.exe")
        windows_codec_lgpl.stage_lgpl_into_bundle(packages, bundle)
        windows_codec_lgpl.assert_release_bundle(bundle)
        archive = shutil.make_archive(str(stage / "Remote-Mac-Python-Windows-x64"), "zip", bundle)
        output = dist / "Remote-Mac-Python-Windows-x64.zip"
        Path(archive).replace(output)
        print(output)
        build_installer(bundle, dist)


if __name__ == "__main__":
    main()
