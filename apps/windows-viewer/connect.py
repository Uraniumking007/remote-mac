"""Start the portable Windows viewer.

The pairing key stays in Windows Credential Manager. The Mac address and
certificate fingerprint are saved in the per-user connection file.
"""

import ipaddress
import json
import os
from pathlib import Path
import sys

import viewer


def user_config_path():
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "Remote Mac" / "connection.json"
    return Path.home() / ".config" / "Remote Mac" / "connection.json"


def parse_fingerprint(value):
    try:
        fingerprint = bytes.fromhex(str(value).replace(":", "").strip())
    except ValueError as error:
        raise ValueError("Enter the SHA-256 LAN certificate fingerprint") from error
    if len(fingerprint) != 32:
        raise ValueError("Enter the SHA-256 LAN certificate fingerprint")
    return fingerprint.hex()


def parse_address(value, *, private):
    try:
        address = ipaddress.IPv4Address(str(value).strip())
    except ValueError as error:
        raise ValueError("Enter the Mac's private LAN IPv4 address") from error
    if private and (not address.is_private or address.is_unspecified or address.is_loopback):
        raise ValueError("Enter the Mac's private LAN IPv4 address")
    return str(address)


def read_config(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "address" not in data or "fingerprint" not in data:
        raise ValueError("Connection config needs an address and fingerprint")
    return {
        "address": parse_address(data["address"], private=False),
        "fingerprint": parse_fingerprint(data["fingerprint"]),
    }


def resolve_connection(user_path, bundled_path, prompt):
    """Return connection settings and whether a new user config was written."""
    if user_path.is_file():
        try:
            return read_config(user_path), False
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ValueError(f"Could not use saved connection config {user_path}: {error}") from error
    if bundled_path.is_file():
        try:
            return read_config(bundled_path), False
        except (OSError, ValueError, KeyError, TypeError):
            pass
    address, fingerprint = prompt()
    config = {
        "address": parse_address(address, private=True),
        "fingerprint": parse_fingerprint(fingerprint),
    }
    user_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = user_path.with_name(user_path.name + ".tmp")
    temporary.write_text(json.dumps(config), encoding="ascii")
    if os.name == "posix":
        os.chmod(temporary, 0o600)
    temporary.replace(user_path)
    return config, True


CONNECTION_PROMPT = (
    "Copy the LAN address and certificate fingerprint from the Mac menu under Connection. "
    "The pairing key is separate and is not asked here."
)


def prompt_connection():
    print(CONNECTION_PROMPT, file=sys.stderr)
    return input("Mac LAN IPv4 address: "), input("Certificate fingerprint: ")


def main():
    try:
        config, created = resolve_connection(
            user_config_path(), Path(__file__).with_name("connection.json"), prompt_connection
        )
    except (EOFError, KeyboardInterrupt):
        raise SystemExit(1)
    except ValueError as error:
        raise SystemExit(str(error))
    if created:
        print(f"Saved connection settings to {user_config_path()}", file=sys.stderr)
    sys.argv[1:1] = ["desktop", config["address"], "--fingerprint", config["fingerprint"]]
    viewer.main()


if __name__ == "__main__":
    main()
