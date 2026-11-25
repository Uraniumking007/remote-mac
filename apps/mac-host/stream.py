"""Run the Mac desktop host."""

import argparse
import getpass
import ipaddress
import json
import math
import os
from pathlib import Path
import select
import socket
import ssl
import subprocess
import threading
import time

from remote_mac_protocol import (
    AUTH_SECONDS, CHUNK_SIZE, CONTROL_PORT, MAX_MESSAGE, PORT,
    authenticate_host, receive_message, remaining_time, send_message, tailnet_ip,
)


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("address")
    parser.add_argument("viewer", nargs="?")
    parser.add_argument("--lan-cert", type=Path)
    parser.add_argument("--lan-key", type=Path)
    parser.add_argument("--device-key-file", type=Path)
    parser.add_argument("--capture", type=Path, default=Path(__file__).with_name("capture"))
    parser.add_argument("--control", type=Path)
    args = parser.parse_args()
    tls = None
    try:
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
        configured = args.device_key_file.read_text(encoding="ascii") if args.device_key_file else None
        key = bytes.fromhex(configured or os.environ.pop("REMOTE_MAC_KEY", None) or getpass.getpass("Pairing key: "))
    except (ValueError, OSError, argparse.ArgumentTypeError) as error:
        parser.error(str(error))
    if len(key) != 32:
        parser.error("Use a 64-character hexadecimal key")
    for executable in (args.capture, args.control):
        if executable is not None and (not executable.is_file() or not os.access(executable, os.X_OK)):
            parser.error(f"Helper is missing or not executable: {executable}")
    try:
        if args.control:
            serve_with_control(args.address, args.viewer, str(args.capture.resolve()), str(args.control.resolve()), key, tls)
        else:
            serve(args.address, args.viewer, str(args.capture.resolve()), key, tls)
    except KeyboardInterrupt:
        pass
    except OSError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
