"""Connect the Windows viewer to a Mac."""

import argparse
import getpass
import ipaddress
import os
import shutil
import socket
import subprocess
import sys

from remote_mac_protocol import PORT, CHUNK_SIZE, authenticate_viewer, tailnet_ip


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def view(host, ffplay, key):
    with socket.create_connection((host, PORT), timeout=5) as connection:
        authenticate_viewer(connection, key)
        connection.settimeout(1)
        command = [
            ffplay, "-hide_banner", "-loglevel", "warning", "-f", "h264",
            "-probesize", "32768", "-analyzeduration", "1",
            "-flags", "low_delay", "-framedrop", "-an",
            "-sync", "video", "-window_title", "Remote Mac prototype", "-"
        ]
        with subprocess.Popen(command, stdin=subprocess.PIPE, bufsize=0) as process:
            try:
                while process.poll() is None:
                    try:
                        chunk = connection.recv(CHUNK_SIZE)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise ConnectionError("Host ended the stream")
                    remaining = memoryview(chunk)
                    while remaining:
                        written = process.stdin.write(remaining)
                        remaining = remaining[written:]
            except BrokenPipeError:
                pass
            finally:
                stop(process)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    baseline = commands.add_parser("view", help="View-only Tailscale baseline using ffplay")
    baseline.add_argument("address", type=tailnet_ip)
    baseline.add_argument("--ffplay", default="ffplay")
    desktop = commands.add_parser("desktop", help="View and control the Mac")
    desktop.add_argument("address")
    desktop.add_argument("--fingerprint")
    desktop.add_argument("--replace-key", action="store_true", help="Prompt for a new saved key")
    args = parser.parse_args()
    try:
        if args.mode == "desktop":
            if args.fingerprint:
                args.address = str(ipaddress.IPv4Address(args.address))
                args.fingerprint = bytes.fromhex(args.fingerprint.replace(":", ""))
                if len(args.fingerprint) != 32:
                    parser.error("Enter the SHA-256 LAN certificate fingerprint")
            else:
                args.address = tailnet_ip(args.address)
    except (ValueError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))
    if args.mode == "view":
        executable = shutil.which(args.ffplay)
        if executable is None:
            parser.error("Install an FFmpeg build with ffplay and put it on PATH.")
    vault = None
    saved = None
    account = args.address
    if args.mode == "desktop" and sys.platform == "win32":
        try:
            from keyring.backends.Windows import WinVaultKeyring
        except ImportError:
            parser.error("Install keyring to save the key in Windows Credential Manager")
        vault = WinVaultKeyring()
        if args.fingerprint:
            account += "/" + args.fingerprint.hex()
        if not args.replace_key:
            saved = vault.get_password("Remote Mac Paired Viewer", account)

    try:
        key = bytes.fromhex(saved or os.environ.pop("REMOTE_MAC_KEY", None) or getpass.getpass("Pairing key: "))
    except ValueError:
        parser.error("Use a 64-character hexadecimal key")
    if len(key) != 32:
        parser.error("Use a 64-character hexadecimal key")

    try:
        if args.mode == "view":
            view(args.address, executable, key)
        else:
            import desktop_viewer

            def remember_key():
                if vault is not None and saved is None:
                    vault.set_password("Remote Mac Paired Viewer", account, key.hex())

            desktop_viewer.run(args.address, key, args.fingerprint, remember_key)
    except KeyboardInterrupt:
        pass
    except OSError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
