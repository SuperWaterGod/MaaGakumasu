import os
import json
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from tempfile import NamedTemporaryFile
from collections import defaultdict
from urllib.request import urlopen

MASTER_URL = "https://raw.githubusercontent.com/vertesan/gakumasu-diff/main"
TRANSLATION_URL = "https://raw.githubusercontent.com/chinosk6/GakumasTranslationData/main/local-files/masterTrans"
CARD_TYPES = ("SSR", "SR", "R")
JAPAN_TIME = timezone(timedelta(hours=9))
EXAM_EFFECTS = {
    "ProduceExamEffectType_ExamParameterBuff": "感性·好调",
    "ProduceExamEffectType_ExamLessonBuff": "感性·集中",
    "ProduceExamEffectType_ExamCardPlayAggressive": "理性·干劲",
    "ProduceExamEffectType_ExamReview": "理性·好印象",
    "ProduceExamEffectType_ExamConcentration": "非凡·强气",
    "ProduceExamEffectType_ExamFullPower": "非凡·全力",
    "ProduceExamEffectType_ExamPreservation": "非凡·温存",
}

preference = {
    "冰渡香名江": {"first": "Vi", "second": "Vo"},
    "雨夜燕": {"first": "Da", "second": "Vo"},
    "藤田琴音": {"first": "Da", "second": "Vi"},
    "葛城莉莉娅": {"first": "Vi", "second": "Da"},
    "花海咲季": {"first": "Vi", "second": "Da"},
    "花海佑芽": {"first": "Da", "second": "Vo"},
    "紫云清夏": {"first": "Da", "second": "Vi"},
    "筱泽广": {"first": "Vo", "second": "Da"},
    "秦谷美铃": {"first": "Vo", "second": "Vi"},
    "有村麻央": {"first": "Vo", "second": "Vi"},
    "月村手毬": {"first": "Vo", "second": "Da"},
    "十王星南": {"first": "Vi", "second": "Vo"},
    "仓本千奈": {"first": "Da", "second": "Vi"},
    "姬崎莉波": {"first": "Vi", "second": "Da"},
}

ATTRIBUTES = ("Vo", "Da", "Vi")
EFFECTS = ("感性·好调", "感性·集中", "理性·干劲", "理性·好印象", "非凡·强气", "非凡·全力", "非凡·温存")
CUSTOM_SONG_CASE = "自定义歌曲"


def add_custom_song_option(options, key_name, idol_name, idol_cn, cn_mode=False):
    """只添加自定义歌曲配置，保留已有卡片、顺序和默认选择。"""
    suffix = "-zh_CN" if cn_mode else ""
    pref = preference.get(idol_cn, {"first": "Vi", "second": "Da"})
    song_key = f"{key_name}自定义歌曲名称"
    first_key = f"自定义歌曲第一属性-{pref['first']}-{pref['second']}{suffix}"

    options[song_key] = {
        "type": "input",
        "label": "$自定义歌曲名称",
        "description": "$自定义歌曲名称说明",
        "inputs": [
            {
                "name": "song_name",
                "label": "$自定义歌曲名称",
                "default": "",
                "pipeline_type": "string",
                "verify": r"^[^\r\n]*\S[^\r\n]*$(?![\r\n])",
                "pattern_msg": "$自定义歌曲名称输入框说明",
            }
        ],
        "pipeline_override": {
            "ProduceChooseIdol": {"custom_recognition_param": {"idol_name": idol_name, "song_name": "{song_name}"}}
        },
    }

    first_cases = []
    for first in ATTRIBUTES:
        remaining = [attr for attr in ATTRIBUTES if attr != first]
        default_second = pref["second"] if pref["second"] in remaining else remaining[0]
        second_key = f"自定义歌曲第二属性-{first}-{default_second}{suffix}"
        second_cases = []
        for second in remaining:
            effect_key = f"自定义歌曲推荐效果-{first}-{second}{suffix}"
            options[effect_key] = {
                "type": "select",
                "label": "$自定义歌曲推荐效果",
                "description": "$自定义歌曲推荐效果说明",
                "default_case": "感性·好调",
                "cases": [
                    {
                        "name": effect,
                        "label": f"${effect}",
                        # 框架会整体替换 custom_action_param，末级选项必须一次写入完整配置。
                        "pipeline_override": {
                            "ProduceChooseNIAEventFlag": {"custom_action_param": {"effect": effect, "first": first, "second": second}}
                        },
                    }
                    for effect in EFFECTS
                ],
            }
            second_cases.append({"name": second, "label": f"$培育属性{second}", "option": [effect_key]})
        options[second_key] = {
            "type": "select",
            "label": "$自定义歌曲第二属性",
            "default_case": default_second,
            "cases": second_cases,
        }
        first_cases.append(
            {
                "name": first,
                "label": f"$培育属性{first}",
                "option": [second_key],
            }
        )
    options[first_key] = {
        "type": "select",
        "label": "$自定义歌曲第一属性",
        "default_case": pref["first"],
        "cases": first_cases,
    }

    # 重复同步时替换自定义入口，始终放在普通卡片末尾。
    config = options[key_name]
    config["cases"] = [case for case in config["cases"] if case["name"] != CUSTOM_SONG_CASE]
    config["cases"].append({"name": CUSTOM_SONG_CASE, "label": "$自定义歌曲", "option": [song_key, first_key]})


def get_project_path(*relative_parts):
    """获取项目根目录下的文件路径"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(script_dir, "..", *relative_parts)


def safe_print(text):
    """安全打印，处理编码问题"""
    try:
        print(text)
    except UnicodeEncodeError:
        # 如果打印失败，尝试用ASCII安全字符替换
        print(text.encode("gbk", errors="replace").decode("gbk"))


def download_table(url, parse):
    try:
        with urlopen(url, timeout=30) as response:
            rows = parse(response.read())
        if not isinstance(rows, list) or not rows:
            raise ValueError("表为空或格式有误")
        indexed = {row["id"]: row for row in rows}
        if len(indexed) != len(rows) or any(not isinstance(key, str) or not key.strip() for key in indexed):
            raise ValueError("表中存在重复或空 ID")
        return indexed
    except Exception as exc:
        raise ValueError(f"读取 {url} 失败：{exc}") from exc


def fetch_cards_data(old_data):
    try:
        import yaml
    except ImportError as exc:
        raise ValueError("读取上游 YAML 需要 PyYAML，请运行 python -m pip install PyYAML") from exc

    cards = download_table(f"{MASTER_URL}/IdolCard.yaml", yaml.safe_load)
    characters = download_table(f"{MASTER_URL}/Character.yaml", yaml.safe_load)
    card_translations = download_table(f"{TRANSLATION_URL}/IdolCard.json", lambda raw: json.loads(raw)["data"])
    character_translations = download_table(f"{TRANSLATION_URL}/Character.json", lambda raw: json.loads(raw)["data"])

    # 保留上游暂未收录的本地卡片，以及相同日期的现有顺序。
    cards_by_type = {rarity: {card["卡片名称"]: card for card in old_data.get(rarity, [])} for rarity in CARD_TYPES}
    local_idol_names = {card["偶像名称"]: card["偶像中文"] for group in cards_by_type.values() for card in group.values()}
    now = datetime.now(JAPAN_TIME)
    missing_translations = []
    for card_id, card in cards.items():
        character_id = card["characterId"]
        character = characters[character_id]
        if not character["isPlayable"]:
            continue
        rarity = card["rarity"].removeprefix("IdolCardRarity_").upper()
        current_cards = cards_by_type[rarity]
        idol_name = character["lastName"] + character["firstName"]
        song_name = card["name"]
        card_name = f"{idol_name}({song_name})"
        previous = current_cards.get(card_name, {})
        timestamp = int(card["viewStartTime"])
        release = datetime.fromtimestamp(timestamp / 1000, JAPAN_TIME) if timestamp else None
        if not previous and release and release > now:
            continue

        translated_character = character_translations.get(character_id, {})
        translated_idol = "".join(translated_character.get(part) or character[part] for part in ("lastName", "firstName"))
        translated_song = (card_translations.get(card_id, {}).get("name") or "").strip()
        song_cn = translated_song or previous.get("歌曲中文", "")
        if not song_cn:
            missing_translations.append(card_name)

        effect = card["showExamEffectType"]
        if effect == "ProduceExamEffectType_Unknown":
            effect = card["examEffectType"]
        # 历史属性含攻略站的养成加成。新卡使用主数据的初始值。
        current_cards[card_name] = {
            "卡片名称": card_name,
            "偶像名称": idol_name,
            "歌曲名称": song_name,
            "偶像中文": local_idol_names.get(idol_name) or translated_idol,
            "歌曲中文": song_cn,
            "推荐效果": EXAM_EFFECTS[effect],
            "Vo": previous.get("Vo", card["produceVocal"]),
            "Da": previous.get("Da", card["produceDance"]),
            "Vi": previous.get("Vi", card["produceVisual"]),
            "体力": previous.get("体力", card["produceStamina"]),
            "奖励加成": previous.get("奖励加成", card["produceVisualGrowthRatePermil"] / 10),
            "登场日期": previous.get("登场日期") or (release.strftime("%Y/%m/%d") if release else ""),
        }

    if missing_translations:
        safe_print("以下卡片暂无简中译名，中文任务将使用日文原名：")
        for card_name in missing_translations:
            safe_print(f"  - {card_name}")
    updated = {
        rarity: sorted(group.values(), key=lambda card: (card["偶像名称"], card["登场日期"]), reverse=True)
        for rarity, group in cards_by_type.items()
    }
    saved_date = now.strftime("%Y/%m/%d")
    if all(updated[rarity] == old_data.get(rarity) for rarity in CARD_TYPES):
        saved_date = old_data.get("保存时间", saved_date)
    return {"保存时间": saved_date, **updated}


def format_cards_data(idols_cards_data, interface_data, cn_mode=False):
    """根据同一份卡片数据生成日文或简中培育选项。"""

    # 确保option字段存在
    if "option" not in interface_data:
        interface_data["option"] = {}

    # 按偶像名称分组卡片
    idol_cards = defaultdict(list)

    for card_type in CARD_TYPES:
        if card_type in idols_cards_data:
            for card in idols_cards_data[card_type]:
                if cn_mode:
                    idol_name = card["偶像中文"]
                    # 尚未核实汉化卡名时保留日文原名，避免生成空名称的 OCR 条件。
                    song_name = card["歌曲中文"] or card["歌曲名称"]
                    card_name = f"{idol_name}({song_name})"
                else:
                    idol_name = card["偶像名称"]
                    card_name = card["卡片名称"]
                    song_name = card["歌曲名称"]
                date_str = card["登场日期"]

                # 解析日期
                try:
                    card_date = datetime.strptime(date_str, "%Y/%m/%d")
                except (TypeError, ValueError):
                    card_date = datetime.min

                idol_cards[idol_name].append(
                    {
                        "card_name": card_name,
                        "idol_name": idol_name,
                        "idol_jp": card["偶像名称"],
                        "idol_cn": card["偶像中文"],
                        "song_name": song_name,
                        "effect": card.get("推荐效果", ""),
                        "date": card_date,
                    }
                )

    # 记录新增的卡片
    new_cards_log = defaultdict(list)
    updated_idols = []

    # 为每个偶像生成或更新配置
    for idol_name, cards in idol_cards.items():
        if cn_mode:
            key_name = f"{idol_name}卡片-zh_CN"
        else:
            key_name = f"{idol_name}卡片"

        # 按日期排序，最新的在前
        cards_sorted = sorted(cards, key=lambda x: x["date"], reverse=True)

        # 去重
        seen_cards = set()
        unique_cards = []
        for card in cards_sorted:
            if card["card_name"] not in seen_cards:
                seen_cards.add(card["card_name"])
                unique_cards.append(card)

        existing_config = interface_data["option"].get(key_name)

        # 生成新的cases
        new_cases = []
        for card in unique_cards:
            idol_cn = card.get("idol_cn", "")
            effect = card.get("effect", "")
            pref = preference.get(idol_cn, {})
            new_cases.append(
                {
                    "name": card["card_name"],
                    "pipeline_override": {
                        "ProduceChooseIdol": {"custom_recognition_param": {"idol_name": card["idol_name"], "song_name": card["song_name"]}},
                        "ProduceChooseNIAEventFlag": {
                            "custom_action_param": {
                                "effect": effect,
                                "first": pref.get("first", ""),
                                "second": pref.get("second", ""),
                            }
                        },
                    },
                }
            )

        # 检查是否已存在该偶像的配置
        if existing_config is not None:
            # 已存在，进行增量更新
            existing_card_names = {case.get("label", case["name"]) for case in existing_config.get("cases", [])}

            # 找出新增的卡片
            for case in new_cases:
                if case["name"] not in existing_card_names:
                    new_cards_log[idol_name].append(case["name"])

            # 更新cases（保持新卡片在前的顺序）
            existing_config["cases"] = new_cases

            # 更新default_case为最新的卡片
            existing_config["default_case"] = new_cases[0]["name"]

            if new_cards_log[idol_name]:
                updated_idols.append(idol_name)
        else:
            # 不存在，新建配置
            interface_data["option"][key_name] = {
                "type": "select",
                "default_case": new_cases[0]["name"],
                "cases": new_cases,
                "label": f"${unique_cards[0]['idol_jp']}卡片",
            }

            # 记录所有卡片为新增
            new_cards_log[idol_name] = [card["card_name"] for card in unique_cards]
            updated_idols.append(idol_name)

        # 普通卡片同步与统计完成后再添加，避免自定义入口参与排序和默认选择。
        add_custom_song_option(interface_data["option"], key_name, idol_name, unique_cards[0]["idol_cn"], cn_mode)

        # 新偶像还需要挂到父选项，否则生成的卡片列表无法在客户端选择。
        parent_key = "培育偶像-zh_CN" if cn_mode else "培育偶像"
        parent = interface_data["option"].get(parent_key)
        if parent is not None and not any(key_name in case.get("option", []) for case in parent["cases"]):
            parent["cases"].append({"name": idol_name, "label": f"${unique_cards[0]['idol_cn']}", "option": [key_name]})

    # 打印结果
    safe_print(f"\n{'=' * 60}")
    safe_print("培育选项已生成")
    safe_print(f"{'=' * 60}")

    if updated_idols:
        safe_print(f"\n共有 {len(updated_idols)} 位偶像的卡片有更新：")
        for idol_name in sorted(updated_idols):
            safe_print(f"\n【{idol_name}】")
            if new_cards_log[idol_name]:
                safe_print(f"  新增 {len(new_cards_log[idol_name])} 张卡片：")
                for card_name in new_cards_log[idol_name]:
                    safe_print(f"    - {card_name}")
    else:
        safe_print("\n没有新增卡片")

    # 显示总体统计
    safe_print(f"\n{'=' * 60}")
    safe_print("总体统计：")

    # 只统计偶像卡片配置，排除自定义入口及其子选项。
    idol_card_options = {k: v for k, v in interface_data["option"].items() if k.endswith(("卡片", "卡片-zh_CN"))}

    safe_print(f"  偶像总数: {len(idol_card_options)} 位")
    total_cards = sum(case["name"] != CUSTOM_SONG_CASE for config in idol_card_options.values() for case in config["cases"])
    safe_print(f"  卡片总数: {total_cards} 张")
    total_new_cards = sum(len(cards) for cards in new_cards_log.values())
    safe_print(f"  本次新增: {total_new_cards} 张")
    safe_print(f"{'=' * 60}\n")
    return interface_data


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def replace_json(path, data):
    pending = NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path), delete=False)
    try:
        with pending as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(pending.name, path)
    finally:
        Path(pending.name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="从 GitHub 同步偶像卡及简中汉化，并生成培育选项")
    parser.add_argument("--local", action="store_true", help="跳过下载，使用本地 idols_cards.json 生成培育选项")
    args = parser.parse_args()

    idols_cards_path = get_project_path("assets", "data", "idols_cards.json")
    old_data = read_json(idols_cards_path)

    try:
        cards_data = old_data if args.local else fetch_cards_data(old_data)
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"偶像卡同步失败：{exc}") from exc

    # 两份任务配置都生成成功后再写盘，本地卡库最后写入。
    outputs = {}
    for filename, cn_mode in (("produce.json", False), ("produce_cn.json", True)):
        path = get_project_path("assets", "tasks", filename)
        safe_print(f"正在生成 {filename}")
        try:
            interface_data = read_json(path)
        except FileNotFoundError:
            interface_data = {}
        updated = format_cards_data(cards_data, interface_data, cn_mode)
        outputs[path] = updated
    if cards_data != old_data:
        outputs[idols_cards_path] = cards_data

    for path, data in outputs.items():
        if not os.path.exists(path) or read_json(path) != data:
            replace_json(path, data)
            safe_print(f"已更新 {path}")


if __name__ == "__main__":
    main()
