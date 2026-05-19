"""Generate MaaGakumasu produce skill-card metadata from gakumas-data.

The upstream source of truth is the gakumas-data package in gakumas-tools:
https://github.com/surisuririsu/gakumas-tools/tree/master/packages/gakumas-data
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "assets" / "data" / "produce_skill_cards.json"
DEFAULT_LOCAL_GAKUMAS_DATA = REPO_ROOT / ".tmp" / "gakumas-tools" / "packages" / "gakumas-data"
SOURCE_REPOSITORY = "https://github.com/surisuririsu/gakumas-tools/tree/master/packages/gakumas-data"


def _parse_csv_int_list(value: Any) -> list[int]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        parsed = []
        for item in value:
            try:
                parsed.append(int(item))
            except (TypeError, ValueError):
                continue
        return parsed
    parsed = []
    for item in str(value).split(","):
        item = item.strip()
        if not item:
            continue
        try:
            parsed.append(int(item))
        except ValueError:
            continue
    return parsed


def _coerce_limit(value: Any) -> Optional[Union[int, str]]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return str(value)


def _coerce_int_or_none(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normal_name(name: str, upgraded: bool) -> str:
    if upgraded and name.endswith("+"):
        return name[:-1]
    return name


def _base_source_id(row: dict[str, Any], rows_by_id: dict[int, dict[str, Any]]) -> int:
    source_id = int(row["id"])
    if not row.get("upgraded"):
        return source_id
    previous = rows_by_id.get(source_id - 1)
    if not previous:
        return source_id
    if previous.get("upgraded"):
        return source_id
    if _normal_name(str(row.get("name", "")), True) == _normal_name(str(previous.get("name", "")), False):
        return int(previous["id"])
    return source_id


def _source_revision(source_dir: Path) -> Optional[str]:
    git_dir = source_dir
    while git_dir != git_dir.parent:
        if (git_dir / ".git").exists():
            try:
                return subprocess.check_output(
                    ["git", "-c", f"safe.directory={git_dir}", "-C", str(git_dir), "rev-parse", "HEAD"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                ).strip()
            except (OSError, subprocess.CalledProcessError):
                return None
        git_dir = git_dir.parent
    return None


def build_database(rows: list[dict[str, Any]], *, source_path: str, source_revision: Optional[str] = None) -> dict[str, Any]:
    rows_by_id = {int(row["id"]): row for row in rows}
    cards = []
    for row in sorted(rows, key=lambda item: int(item["id"])):
        source_id = int(row["id"])
        upgraded = bool(row.get("upgraded"))
        base_id = _base_source_id(row, rows_by_id)
        variant = "upgraded" if upgraded else "normal"
        card_id = f"skill_card_{source_id:04d}"
        base_card_id = f"skill_card_{base_id:04d}"
        name = str(row.get("name", ""))
        template_path = f"resource/base/image/produce/skill_cards/{card_id}/{variant}.png"
        cards.append(
            {
                "id": card_id,
                "source_id": source_id,
                "base_id": base_card_id,
                "base_source_id": base_id,
                "variant": variant,
                "name": name,
                "name_normalized": _normal_name(name, upgraded),
                "rarity": row.get("rarity") or "",
                "type": row.get("type") or "",
                "plan": row.get("plan") or "",
                "unlock_plv": _coerce_int_or_none(row.get("unlockPlv")),
                "unique": bool(row.get("unique")),
                "source_type": row.get("sourceType") or "",
                "p_idol_id": _coerce_int_or_none(row.get("pIdolId")),
                "force_initial_hand": bool(row.get("forceInitialHand")),
                "available_customizations": _parse_csv_int_list(row.get("availableCustomizations")),
                "effect_dsl": {
                    "conditions": row.get("conditions") or "",
                    "cost": row.get("cost") or "",
                    "actions": row.get("actions") or "",
                    "limit": _coerce_limit(row.get("limit")),
                    "effects": row.get("effects") or "",
                },
                "template_path": template_path,
            }
        )

    return {
        "schema_version": 1,
        "source": {
            "name": "gakumas-data",
            "repository": SOURCE_REPOSITORY,
            "source_path": source_path,
            "source_revision": source_revision,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "record_count": len(cards),
        },
        "cards": cards,
    }


def _resolve_source_dir(value: str | None) -> Path:
    if value:
        return Path(value)
    env_value = os.environ.get("GAKUMAS_DATA_DIR")
    if env_value:
        return Path(env_value)
    return DEFAULT_LOCAL_GAKUMAS_DATA


def load_rows(source_dir: Path) -> list[dict[str, Any]]:
    source_file = source_dir / "json" / "skill_cards.json"
    if not source_file.exists():
        raise FileNotFoundError(f"gakumas-data skill_cards.json not found: {source_file}")
    with source_file.open("r", encoding="utf-8") as fp:
        rows = json.load(fp)
    if not isinstance(rows, list):
        raise ValueError(f"Expected list in {source_file}")
    return rows


def write_database(database: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as fp:
        json.dump(database, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gakumas-data-dir", help="Path to packages/gakumas-data.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output JSON path.")
    args = parser.parse_args()

    source_dir = _resolve_source_dir(args.gakumas_data_dir).resolve()
    output_path = Path(args.output).resolve()
    rows = load_rows(source_dir)
    source_path = "packages/gakumas-data/json/skill_cards.json" if source_dir.name == "gakumas-data" else str(source_dir)
    database = build_database(rows, source_path=source_path, source_revision=_source_revision(source_dir))
    write_database(database, output_path)
    print(f"Wrote {len(database['cards'])} skill cards to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
