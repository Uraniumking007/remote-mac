"""Start the portable Windows viewer using its bundled settings."""

import json
from pathlib import Path
import sys

import viewer


def main():
    config_file = Path(__file__).with_name("connection.json")
    config = json.loads(config_file.read_text(encoding="utf-8"))
    sys.argv[1:1] = ["desktop", config["address"], "--fingerprint", config["fingerprint"]]
    viewer.main()


if __name__ == "__main__":
    main()
