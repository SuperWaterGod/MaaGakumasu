"""构建时关联游戏原名与插件汉化，向现有数据库写入中文别名及来源主键。"""

import re
import json
import subprocess
from difflib import SequenceMatcher
from pathlib import Path
from collections import defaultdict

from produce.catalog import split_variant, normalize_name, name_match_allowed
from produce.resources import file_hash

TABLES = ("ProduceCard", "ProduceDrink", "ProduceItem", "ProduceCustomizeItem", "SupportCard")
PLANS = {"sense": "ProducePlanType_Plan1", "logic": "ProducePlanType_Plan2", "anomaly": "ProducePlanType_Plan3"}
GAME_REPOSITORY = "https://github.com/vertesan/gakumasu-diff"
TRANSLATION_REPOSITORY = "https://github.com/chinosk6/GakumasTranslationData"


def selected_sources(game_source, translation_source):
    """所有关联输入都参与资源指纹，汉化单独更新也能触发重新构建。"""
    selected = {}
    for table in TABLES:
        selected[f"game/{table}.yaml"] = Path(game_source) / f"{table}.yaml"
        selected[f"translation/{table}.json"] = Path(translation_source) / "local-files/masterTrans" / f"{table}.json"
    selected["builder/translations.py"] = Path(__file__)
    selected["builder/catalog.py"] = Path(__file__).resolve().parents[1] / "agent/produce/catalog.py"
    for path in selected.values():
        if not path.is_file():
            raise ValueError(f"汉化来源文件缺失: {path}")
    return selected


def source_info(directory, repository):
    directory = str(Path(directory).resolve())
    revision = subprocess.check_output(["git", "-C", directory, "rev-parse", "HEAD"], text=True).strip()
    timestamp = subprocess.check_output(["git", "-C", directory, "show", "-s", "--format=%cI", "HEAD"], text=True).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("汉化来源提交无效")
    return {"repository": repository, "revision": revision, "timestamp": timestamp}


def read_original(path):
    """仅解析顶层主键、名称和流派，避免效果正文中的非标准 YAML 影响名称关联。"""
    import yaml

    source = path.read_text(encoding="utf-8-sig")
    headers = re.findall(r"(?m)^(?:- id:.*|  (?:name|upgradeCount|planType):.*)$", source)
    rows = yaml.safe_load("\n".join(headers))
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"游戏名称表为空: {path}")
    return rows


def row_key(row, table):
    if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
        raise ValueError(f"{table} 缺少游戏 ID")
    key = (row["id"],)
    if table == "ProduceCard":
        if type(row.get("upgradeCount")) is not int or row["upgradeCount"] < 0:
            raise ValueError("技能卡缺少有效的强化次数")
        key += (row["upgradeCount"],)
    return key


def index_rows(rows, table):
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{table} 名称表为空")
    index = {}
    for row in rows:
        key = row_key(row, table)
        if not isinstance(row.get("name"), str) or not row["name"].strip():
            raise ValueError(f"{table} 条目名称为空: {key}")
        if key in index and index[key]["name"] != row["name"]:
            raise ValueError(f"{table} 同一主键有多个名称: {key}")
        index[key] = row
    return index


def match_original(row, originals):
    """完整原名优先，归一化后仍有名称歧义时拒绝；模糊匹配复用客户端容错。"""
    name = row["name"]
    candidates = [item for item in originals if item["name"] == name]
    if candidates:
        return candidates, "exact"
    candidates = [item for item in originals if normalize_name(item["name"]) == normalize_name(name)]
    if candidates:
        return candidates, "normalized"
    base, plus = split_variant(name)
    groups = defaultdict(list)
    for item in originals:
        candidate, count = split_variant(item["name"])
        if plus == count and base.count("+") == candidate.count("+"):
            groups[candidate].append(item)
    ranked = sorted(((SequenceMatcher(None, base, candidate).ratio(), candidate) for candidate in groups), reverse=True)
    if not ranked:
        return [], "missing"
    score, candidate = ranked[0]
    margin = score - ranked[1][0] if len(ranked) > 1 else 1.0
    if not name_match_allowed(name, candidate + "+" * plus, 1.0, score, margin):
        return [], "ambiguous_or_missing"
    return groups[candidate], "fuzzy"


def enrich(skills, items, game_source, translation_source):
    """保留原名和效果，只添加已确认的中文名、别名及正确来源表的主键。"""
    selected = selected_sources(game_source, translation_source)
    originals, translations = {}, {}
    for table in TABLES:
        originals[table] = list(index_rows(read_original(selected[f"game/{table}.yaml"]), table).values())
        translated = json.loads(selected[f"translation/{table}.json"].read_text(encoding="utf-8-sig"))
        translations[table] = index_rows(translated.get("data"), table)
    coverage = {}
    items["support_cards"] = [{"id": row["id"], "name": row["name"]} for row in originals["SupportCard"]]
    for kind, table, rows in (("skill_card", "ProduceCard", skills["cards"]),
                              ("drink", "ProduceDrink", items["drinks"]),
                              ("p_item", "ProduceItem", items["p_items"]),
                              ("support_card", "SupportCard", items["support_cards"])):
        counts = {"records": len(rows), "translated": 0, "fuzzy": 0, "unresolved_ids": []}
        for row in rows:
            matched_table = table
            # 上游少数强化条目漏写加号，以结构化强化字段修正关联查询。
            query = {**row, "name": row["name"] + "+"} if row.get("upgraded") and not split_variant(row["name"])[1] else row
            candidates, method = ([row], "id") if kind == "support_card" else match_original(query, originals[table])
            if kind == "skill_card":
                candidates = [item for item in candidates if item["upgradeCount"] == int(bool(row.get("upgraded")))]
            if not candidates and kind == "p_item":
                matched_table = "ProduceCustomizeItem"
                candidates = [item for item in originals[matched_table]
                              if item["name"] == row["name"] and item.get("planType") == PLANS.get(row.get("plan"))]
                method = "customize_exact_name_and_plan"
            targets = {row_key(item, matched_table) for item in candidates}
            translated = translations[matched_table]
            names = {translated[key]["name"] for key in targets if key in translated}
            if len(names) != 1 or any(key not in translated for key in targets):
                counts["unresolved_ids"].append(row["id"])
                continue
            row["name_zh_cn"] = next(iter(names))
            row["aliases"] = sorted({*row.get("aliases", []), row["name_zh_cn"], *(item["name"] for item in candidates)} - {row["name"]})
            if row.get("upgraded") and not split_variant(row["name_zh_cn"])[1]:
                row["aliases"] = sorted({*row["aliases"], row["name_zh_cn"] + "+"})
            row["game_keys"] = [{"table": matched_table, "id": key[0], **({"upgradeCount": key[1]} if len(key) == 2 else {})}
                                for key in sorted(targets)]
            counts["translated"] += 1
            counts["fuzzy"] += method == "fuzzy"
        coverage[kind] = counts
    metadata = {
        "language": "zh-CN", "game": source_info(game_source, GAME_REPOSITORY),
        "translation": source_info(translation_source, TRANSLATION_REPOSITORY),
        "files": [{"path": name, "sha256": file_hash(path)} for name, path in sorted(selected.items())],
        "coverage": coverage,
    }
    skills["source"]["translations"] = items["source"]["translations"] = metadata
    return metadata
