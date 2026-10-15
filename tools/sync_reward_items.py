"""Fetch pinned drink and P-item catalogs alongside the existing skill-card snapshot."""

import json
import hashlib
import argparse
import urllib.request
from pathlib import Path
from datetime import datetime, timezone

REPOSITORY = "https://github.com/gakumas-tools/gakumas-tools"
TABLES = {"drinks": "p_drinks.json", "p_items": "p_items.json"}


def build_database(tables, revision):
    result = {"schema_version": 1, "source": {"repository": REPOSITORY, "revision": revision,
              "fetched_at": datetime.now(timezone.utc).isoformat(), "license": "BSD-3-Clause; see GAKUMAS_DATA_LICENSE.txt", "tables": {}}}
    for key, (rows, raw_sha) in tables.items():
        if key not in TABLES or not isinstance(rows, list) or not rows:
            raise ValueError("Expected nonempty known item table")
        ids = set()
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), int) or not isinstance(row.get("name"), str) or not row["name"]:
                raise ValueError("Invalid item id/name")
            if row["id"] in ids or not isinstance(row.get("actions" if key == "drinks" else "effects", ""), str):
                raise ValueError("Duplicate item id or invalid effects")
            ids.add(row["id"])
        result[key] = rows
        result["source"]["tables"][key] = {"path": "packages/gakumas-data/json/" + TABLES[key],
                                          "raw_sha256": raw_sha, "record_count": len(rows)}
    if set(tables) != set(TABLES):
        raise ValueError("Both drink and P-item tables are required")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", help="Full upstream commit; defaults to the skill-card snapshot revision")
    parser.add_argument("--skills", type=Path, default=Path("assets/data/produce_skill_cards.json"))
    parser.add_argument("--output", type=Path, default=Path("assets/data/produce_reward_items.json"))
    args = parser.parse_args()
    revision = args.revision or json.loads(args.skills.read_text(encoding="utf-8"))["source"]["revision"]
    if len(revision) != 40 or any(char not in "0123456789abcdef" for char in revision):
        parser.error("revision must be a full lowercase commit SHA")
    prefix = f"https://raw.githubusercontent.com/gakumas-tools/gakumas-tools/{revision}/"
    tables = {}
    for key, filename in TABLES.items():
        with urllib.request.urlopen(prefix + "packages/gakumas-data/json/" + filename, timeout=30) as response:
            raw = response.read()
        tables[key] = (json.loads(raw), hashlib.sha256(raw).hexdigest())
    database = build_database(tables, revision)
    # Preserve the project's existing license file; verify its upstream contents.
    with urllib.request.urlopen(prefix + "LICENSE", timeout=30) as response:
        license_bytes = response.read()
    license_path = args.output.parent / "GAKUMAS_DATA_LICENSE.txt"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if license_path.exists() and license_path.read_text(encoding="utf-8").replace("\r\n", "\n") != license_bytes.decode().replace("\r\n", "\n"):
        raise ValueError("Existing upstream license differs; no overwrite")
    if not license_path.exists():
        license_path.write_bytes(license_bytes)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(database, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8", newline="\n")
    temporary.replace(args.output)
    print(f"Saved {len(database['drinks'])} drinks and {len(database['p_items'])} P-items from {revision}")


if __name__ == "__main__":
    main()
