"""Throwaway, single-viewer H.264 stream over an existing Tailscale tunnel."""

import argparse
import getpass
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import secrets
import select
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time

TAILNET = ipaddress.ip_network("100.64.0.0/10")
PORT = 47332
CONTROL_PORT = 47333
MAX_MESSAGE = 800_000
AUTH_SECONDS = 3
CHUNK_SIZE = 16384


def tailnet_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Enter a Tailscale IPv4 address") from error
    if address not in TAILNET:
        raise argparse.ArgumentTypeError("Enter a Tailscale IPv4 address")
    return str(address)


def remaining_time(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise TimeoutError("Peer deadline exceeded")
    return seconds


def read_exact(connection, count, deadline=None):
    result = bytearray()
    while len(result) < count:
        if deadline is not None:
            connection.settimeout(remaining_time(deadline))
        chunk = connection.recv(count - len(result))
        if not chunk:
            raise ConnectionError("Peer disconnected during authentication")
        result.extend(chunk)
    return bytes(result)


def proof(key, role, nonce):
    return hmac.digest(key, b"remote-mac-v1/" + role + nonce, hashlib.sha256)


def authenticate_host(connection, key, deadline=None):
    if deadline is None:
        deadline = time.monotonic() + AUTH_SECONDS
    connection.settimeout(remaining_time(deadline))
    nonce = secrets.token_bytes(32)
    connection.sendall(nonce)
    if not hmac.compare_digest(read_exact(connection, 32, deadline), proof(key, b"viewer", nonce)):
        raise ConnectionError("Viewer authentication failed")
    connection.settimeout(remaining_time(deadline))
    connection.sendall(proof(key, b"host", nonce))


def authenticate_viewer(connection, key):
    nonce = read_exact(connection, 32)
    connection.sendall(proof(key, b"viewer", nonce))
    if not hmac.compare_digest(read_exact(connection, 32), proof(key, b"host", nonce)):
        raise ConnectionError("Host authentication failed")


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def send_message(connection, message):
    data = json.dumps(message, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_MESSAGE:
        raise ConnectionError("Control message is too large")
    connection.sendall(len(data).to_bytes(4, "big") + data)


def receive_message(connection, deadline=None):
    size = int.from_bytes(read_exact(connection, 4, deadline), "big")
    if not 0 < size <= MAX_MESSAGE:
        raise ConnectionError("Invalid control message length")
    return read_exact(connection, size, deadline)


def authenticated_session(connection, key, tls, session_timeout):
    deadline = time.monotonic() + AUTH_SECONDS
    connection.settimeout(remaining_time(deadline))
    session = tls.wrap_socket(connection, server_side=True) if tls else connection
    try:
        authenticate_host(session, key, deadline)
        session.settimeout(session_timeout)
        return session
    except BaseException:
        session.close()
        raise


def int_between(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def coordinate(value):
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def parse_control(data):
    message = json.loads(data)
    if not isinstance(message, dict) or not isinstance(message.get("kind"), str):
        raise ConnectionError("Invalid control message")
    kind = message["kind"]
    if kind in ("heartbeat", "release", "clipboard-get"):
        fields = {}
    elif kind == "key":
        fields = {"code": lambda v: int_between(v, 0, 127), "down": lambda v: type(v) is bool}
    elif kind in ("move", "button"):
        fields = {"x": coordinate, "y": coordinate}
        if kind == "button":
            fields.update(button=lambda v: int_between(v, 0, 2),
                          down=lambda v: type(v) is bool,
                          clicks=lambda v: int_between(v, 1, 3))
    elif kind == "scroll":
        fields = {axis: lambda v: int_between(v, -120, 120) for axis in ("x", "y")}
    elif kind == "clipboard-set":
        fields = {"text": lambda v: isinstance(v, str) and len(v.encode("utf-8")) <= 128_000}
    elif kind == "video-format":
        fields = {"format": lambda v: v == "framed-h264"}
    elif kind == "display-mode":
        fields = {"mode": lambda v: v in ("mirror", "extend", "primary")}
    else:
        raise ConnectionError("Unknown control message")
    if message.keys() != {"kind", *fields} or not all(check(message[field]) for field, check in fields.items()):
        raise ConnectionError("Invalid control fields")
    return message


def serve_control(listener, viewer, helper, key, selection, tls=None):
    while True:
        connection, (peer, _) = listener.accept()
        with connection:
            if viewer is not None and peer != viewer:
                continue
            try:
                connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                with authenticated_session(connection, key, tls, 8) as session:
                    with subprocess.Popen([helper], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
                        try:
                            if not select.select([process.stdout], [], [], 3)[0]:
                                raise ConnectionError("Control helper did not start")
                            if process.stdout.readline() != b"ready\n":
                                raise ConnectionError("Control helper unavailable; check Accessibility")
                            send_message(session, {"ready": True})
                            while True:
                                message = parse_control(receive_message(session, time.monotonic() + 8))
                                kind = message["kind"]
                                if kind == "heartbeat":
                                    continue
                                if kind == "video-format":
                                    selection["framed"] = True
                                    send_message(session, {"format": "framed-h264"})
                                    continue
                                process.stdin.write(json.dumps(message).encode("ascii") + b"\n")
                                process.stdin.flush()
                                if kind in ("clipboard-get", "display-mode"):
                                    timeout = 4 if kind == "display-mode" else 1
                                    if not select.select([process.stdout], [], [], timeout)[0]:
                                        raise ConnectionError(f"{kind} response timed out")
                                    reply = json.loads(process.stdout.readline(MAX_MESSAGE))
                                    if kind == "display-mode" and reply.get("display"):
                                        selection["display"] = reply["display"]
                                    send_message(session, reply)
                        finally:
                            selection.clear()
                            try:
                                process.stdin.close()
                                process.wait(timeout=2)
                            except (BrokenPipeError, subprocess.TimeoutExpired):
                                stop(process)
            except (OSError, ValueError, RecursionError) as error:
                print(f"Control disconnected: {error}", flush=True)


def serve_with_control(host, viewer, capture, helper, key, tls=None):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, CONTROL_PORT))
        listener.listen(1)
        selection = {}
        threading.Thread(target=serve_control, args=(listener, viewer, helper, key, selection, tls), daemon=True).start()
        serve(host, viewer, capture, key, tls, selection)


def serve(host, viewer, capture, key, tls=None, selection=None):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((host, PORT))
        listener.listen(1)
        print(f"Waiting for {viewer or 'an authenticated LAN viewer'} at {host}:{PORT}", flush=True)
        while True:
            connection, (peer, _) = listener.accept()
            with connection:
                if viewer is not None and peer != viewer:
                    continue
                try:
                    connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    with authenticated_session(connection, key, tls, 3) as session:
                        print(f"Authenticated {peer}", flush=True)
                        display = selection.get("display") if selection else None
                        command = [capture, str(display)] if display is not None else [capture]
                        if selection and selection.get("framed"):
                            command.append("--framed")
                        with subprocess.Popen(command, stdout=subprocess.PIPE, bufsize=0) as process:
                            try:
                                while True:
                                    readable, _, _ = select.select([session, process.stdout], [], [])
                                    if session in readable:
                                        break
                                    chunk = process.stdout.read(CHUNK_SIZE)
                                    if not chunk:
                                        break
                                    session.sendall(chunk)
                            finally:
                                stop(process)
                            if process.returncode:
                                print(f"Capture exited with status {process.returncode}", flush=True)
                except OSError as error:
                    print(f"Session ended: {error}", flush=True)


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
    host = commands.add_parser("host")
    host.add_argument("address")
    host.add_argument("viewer", nargs="?")
    host.add_argument("--lan-cert", type=Path)
    host.add_argument("--lan-key", type=Path)
    host.add_argument("--device-key-file", type=Path)
    host.add_argument("--capture", type=Path, default=Path(__file__).with_name("capture"))
    host.add_argument("--control", type=Path, help="Path to the compiled Control.swift helper")
    viewer = commands.add_parser("view")
    viewer.add_argument("address", type=tailnet_ip)
    viewer.add_argument("--ffplay", default="ffplay")
    desktop = commands.add_parser("desktop", help="View and control the Mac")
    desktop.add_argument("address")
    desktop.add_argument("--fingerprint")
    desktop.add_argument("--replace-key", action="store_true", help="Prompt for a new saved key")
    args = parser.parse_args()
    tls = None
    try:
        if args.mode == "host":
            if args.lan_cert and args.lan_key:
                address = ipaddress.IPv4Address(args.address)
                if not address.is_private or address.is_unspecified or address.is_loopback:
                    parser.error("Enter the Mac's private LAN IPv4 address")
                if args.viewer:
                    args.viewer = str(ipaddress.IPv4Address(args.viewer))
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                tls.minimum_version = ssl.TLSVersion.TLSv1_3
                tls.load_cert_chain(args.lan_cert, args.lan_key)
            elif args.lan_cert or args.lan_key or not args.viewer:
                parser.error("Supply a Tailscale viewer IP, or both LAN certificate files")
            else:
                args.address = tailnet_ip(args.address)
                args.viewer = tailnet_ip(args.viewer)
        elif args.mode == "desktop":
            if args.fingerprint:
                args.address = str(ipaddress.IPv4Address(args.address))
                args.fingerprint = bytes.fromhex(args.fingerprint.replace(":", ""))
                if len(args.fingerprint) != 32:
                    parser.error("Enter the SHA-256 LAN certificate fingerprint")
            else:
                args.address = tailnet_ip(args.address)
    except (ValueError, OSError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))
    if args.mode == "host":
        executable = str(args.capture.resolve())
        if not args.capture.is_file() or not os.access(executable, os.X_OK):
            parser.error("Build the capture executable first. See README.md.")
    elif args.mode == "view":
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
        configured = args.device_key_file.read_text(encoding="ascii") if args.mode == "host" and args.device_key_file else None
        key = bytes.fromhex(configured or saved or os.environ.pop("REMOTE_MAC_KEY", None) or
                            getpass.getpass("Pairing key: "))
    except (ValueError, OSError):
        parser.error("Could not read a valid 64-character hexadecimal key")
    if len(key) != 32:
        parser.error("Use a 64-character hexadecimal key")

    try:
        if args.mode == "host":
            if args.control:
                helper = args.control.resolve()
                if not helper.is_file() or not os.access(helper, os.X_OK):
                    parser.error("Build the control executable first. See README.md.")
                serve_with_control(args.address, args.viewer, executable, str(helper), key, tls)
            else:
                serve(args.address, args.viewer, executable, key, tls)
        elif args.mode == "view":
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
