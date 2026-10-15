#!/usr/bin/env python3
"""Copy private settings/history from an earlier installation without exposing them."""

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    ".env",
    "settings.json",
    "state.json",
    "anime.json",
    "title_overrides.json",
    "cdn_cooldown.json",
)


def starter(path):
    if path.name == "anime.json":
        try:
            data = json.loads(path.read_text())
            return not data.get("anime") and not data.get("jobs")
        except (ValueError, OSError):
            return False
    if path.name == ".env":
        return "DISCORD_TOKEN=YOUR_USER_TOKEN_HERE" in path.read_text()
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", type=Path, required=True)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Back up and replace existing non-empty destination settings/history.",
    )
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    if source == ROOT:
        raise SystemExit("Source and destination are the same directory.")
    names = [name for name in FILES if (source / name).is_file()]
    conflicts = [
        name for name in names if (ROOT / name).exists() and not starter(ROOT / name)
    ]
    if conflicts and not args.replace:
        raise SystemExit(
            "Refusing to overwrite existing data: "
            + ", ".join(conflicts)
            + ". Use --replace only if intentional."
        )
    backup = ROOT / ".migration-backups" / datetime.now().strftime("%Y%m%d-%H%M%S")
    backup.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in names:
        destination = ROOT / name
        if destination.exists():
            shutil.copy2(destination, backup / name)
        shutil.copyfile(source / name, destination)
        destination.chmod(0o600)
        print("Copied", name, "(contents not displayed)")
    print(
        "Migration complete. Legacy state.json is imported into anime.json when the bot starts."
    )
    print(
        "Old CDN_REQUEST_INTERVAL is unused; ANIMAX uses REQUEST_INTERVAL=0.25 unless you explicitly configure otherwise."
    )
    print("Stop the old bot before starting ANIMAX. Backups:", backup)


if __name__ == "__main__":
    main()
