"""Wire format and authentication shared by the host and Windows viewer."""

import argparse
import hmac
import ipaddress
import json
import secrets
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
