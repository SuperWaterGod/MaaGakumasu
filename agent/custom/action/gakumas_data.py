from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Union

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


SOURCE_REPOSITORY = "https://github.com/surisuririsu/gakumas-tools/tree/master/packages/gakumas-data"
SKILL_CARDS_RAW_URL = "https://raw.githubusercontent.com/surisuririsu/gakumas-tools/master/packages/gakumas-data/json/skill_cards.json"


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


def build_skill_card_database(rows: list[dict[str, Any]], *, source_path: str, source_revision: Optional[str] = None) -> dict[str, Any]:
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
                "template_path": f"resource/base/image/produce/skill_cards/{card_id}/{variant}.png",
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


def download_skill_card_rows(url: str = SKILL_CARDS_RAW_URL, timeout: int = 20) -> list[dict[str, Any]]:
    request = urllib.request.Request(url, headers={"User-Agent": "MaaGakumasu"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read()
    rows = json.loads(payload.decode("utf-8"))
    if not isinstance(rows, list):
        raise ValueError("Expected a list from gakumas-data skill_cards.json")
    return rows


def _output_database_path() -> Path:
    cwd = Path.cwd()
    packaged_path = cwd / "data" / "produce_skill_cards.json"
    source_path = cwd / "assets" / "data" / "produce_skill_cards.json"
    if packaged_path.parent.exists():
        return packaged_path
    return source_path


def write_database(database: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as fp:
        json.dump(database, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


def _clear_skill_card_cache() -> None:
    try:
        from . import skill_cards

        skill_cards.load_skill_cards.cache_clear()
        skill_cards._template_hashes.cache_clear()
    except Exception as exc:
        logger.warning(f"Could not clear skill-card cache: {exc}")


@AgentServer.custom_action("UpdateGakumasData")
class UpdateGakumasData(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> CustomAction.RunResult | bool:
        try:
            logger.info("Updating gakumas-data skill-card database")
            rows = download_skill_card_rows()
            database = build_skill_card_database(
                rows,
                source_path="packages/gakumas-data/json/skill_cards.json",
            )
            output_path = _output_database_path()
            write_database(database, output_path)
            _clear_skill_card_cache()
            logger.success(f"Updated {len(database['cards'])} skill cards: {output_path}")
            return True
        except Exception as exc:
            logger.exception(f"Failed to update gakumas-data: {exc}")
            return False
