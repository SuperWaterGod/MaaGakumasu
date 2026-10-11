"""Fetch a pinned gakumas-data skill-card snapshot for offline identity matching."""

import json
import hashlib
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

REPOSITORY = "https://github.com/gakumas-tools/gakumas-tools"


def build_database(rows, revision, raw_sha256):
    if not isinstance(rows, list) or not rows:
        raise ValueError("Expected nonempty skill-card list")
    ids = set()
    for row in rows:
        if not isinstance(row.get("id"), int) or not isinstance(row.get("name"), str) or not row["name"]:
            raise ValueError("Invalid card id/name")
        if row["id"] in ids:
            raise ValueError("Duplicate card ID")
        ids.add(row["id"])
    return {
        "schema_version": 1,
        "source": {
            "repository": REPOSITORY,
            "revision": revision,
            "path": "packages/gakumas-data/json/skill_cards.json",
            "raw_sha256": raw_sha256,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "record_count": len(rows),
            "license": "BSD-3-Clause; see GAKUMAS_DATA_LICENSE.txt",
        },
        "cards": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="Full upstream commit SHA")
    parser.add_argument("--output", type=Path, default=Path("assets/data/produce_skill_cards.json"))
    args = parser.parse_args()
    if len(args.revision) != 40 or any(c not in "0123456789abcdef" for c in args.revision):
        parser.error("revision must be a full lowercase commit SHA")
    prefix = f"https://raw.githubusercontent.com/gakumas-tools/gakumas-tools/{args.revision}/"
    with urllib.request.urlopen(prefix + "packages/gakumas-data/json/skill_cards.json", timeout=30) as response:
        raw = response.read()
    database = build_database(json.loads(raw), args.revision, hashlib.sha256(raw).hexdigest())
    with urllib.request.urlopen(prefix + "LICENSE", timeout=30) as response:
        license_text = response.read().decode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    license_path = args.output.parent / "GAKUMAS_DATA_LICENSE.txt"
    license_path.write_text(license_text, encoding="utf-8", newline="\n")
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(database, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8", newline="\n")
    temporary.replace(args.output)
    print(f"Saved {len(database['cards'])} records from {args.revision} to {args.output}")


if __name__ == "__main__":
    main()
