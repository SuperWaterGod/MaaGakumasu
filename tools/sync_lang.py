#!/usr/bin/env python3
"""
翻译文件同步脚本 v3
支持多 interface 文件 & 目录扫描

使用方式:
    python tools/sync_lang.py
    python tools/sync_lang.py --interfaces a.json b.json
    python tools/sync_lang.py --interfaces assets/interface/
"""

import json
from pathlib import Path
from collections import OrderedDict

try:
    from opencc import OpenCC
    HAS_OPENCC = True
except ImportError:
    HAS_OPENCC = False


# ========================
# 新增：路径解析
# ========================
def resolve_interface_paths(paths: list[Path]) -> list[Path]:
    """解析输入路径，支持文件和目录，返回所有 json 文件"""
    result = []

    for path in paths:
        if path.is_file() and path.suffix == ".json":
            result.append(path)
        elif path.is_dir():
            result.extend(sorted(path.rglob("*.json")))
        else:
            print(f"⚠ 跳过无效路径: {path}")

    if not result:
        raise ValueError("未找到任何有效的 interface json 文件")

    return result


# ========================
# 原有逻辑（基本不动）
# ========================
def _extract_dollar_keys_ordered(value: any, keys: list) -> None:
    if isinstance(value, str):
        if value.startswith("$"):
            key = value[1:]
            if key not in keys:
                keys.append(key)
    elif isinstance(value, list):
        for item in value:
            _extract_dollar_keys_ordered(item, keys)
    elif isinstance(value, dict):
        if "label" in value:
            _extract_dollar_keys_ordered(value["label"], keys)
        if "doc" in value:
            _extract_doc_key_ordered(value["doc"], keys)

        for k, v in value.items():
            if k not in ["label", "doc"]:
                _extract_dollar_keys_ordered(v, keys)


def _extract_doc_key_ordered(doc: any, keys: list) -> None:
    if isinstance(doc, list):
        cleaned_items = []
        for item in doc:
            if isinstance(item, str) and item.startswith("$"):
                cleaned_items.append(item[1:])
            elif isinstance(item, str) and item:
                return

        if cleaned_items:
            merged_key = "\n".join(cleaned_items)
            if merged_key not in keys:
                keys.append(merged_key)

    elif isinstance(doc, str) and doc.startswith("$"):
        key = doc[1:]
        if key not in keys:
            keys.append(key)


# ========================
# 修改：支持多 interface
# ========================
def extract_keys_from_interfaces(interface_paths: list[Path]) -> list:
    """从多个 interface.json 提取 key（保持顺序）"""
    keys = []

    for path in interface_paths:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        _extract_dollar_keys_ordered(data, keys)

    return keys


def _load_translations(lang_path: Path) -> dict:
    if lang_path.exists():
        with open(lang_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_translations(translations: dict, lang_path: Path) -> None:
    with open(lang_path, "w", encoding="utf-8") as f:
        json.dump(translations, f, ensure_ascii=False, indent=4)


def sync_zh_cn(required_keys: list, zh_cn_path: Path, dry_run: bool = False) -> dict:
    existing_translations = _load_translations(zh_cn_path)

    new_translations = OrderedDict()
    added_keys = []

    for key in required_keys:
        if key in existing_translations:
            new_translations[key] = existing_translations[key]
        else:
            new_translations[key] = key
            added_keys.append(key)

    existing_keys = set(existing_translations.keys())
    removed_keys = existing_keys - set(required_keys)

    print("=== zh-CN.json 同步报告 ===")
    print(f"需要: {len(required_keys)} | 现有: {len(existing_keys)}")
    print(f"新增: {len(added_keys)} | 移除: {len(removed_keys)}\n")

    if not dry_run:
        _save_translations(new_translations, zh_cn_path)
        print(f"✓ 已更新 {zh_cn_path.name}\n")

    return new_translations


def get_all_lang_configs():
    return {
        "zh-Hant": {"name": "繁体中文", "converter": "s2twp" if HAS_OPENCC else None},
        "en": {"name": "英文", "converter": None},
        "ja": {"name": "日文", "converter": None},
    }


def translate_to_other_langs(zh_cn_translations, lang_dir, target_langs=None, dry_run=False):
    configs = get_all_lang_configs()

    if target_langs is None:
        target_langs = list(configs.keys())

    for lang in target_langs:
        if lang not in configs:
            continue

        path = lang_dir / f"{lang}.json"
        converter = OpenCC(configs[lang]["converter"]) if configs[lang]["converter"] and HAS_OPENCC else None

        new_data = OrderedDict()
        for k, v in zh_cn_translations.items():
            new_data[k] = converter.convert(v) if converter else v

        if not dry_run:
            _save_translations(new_data, path)

        print(f"✓ {lang}.json 已同步")


# ========================
# main
# ========================
def main():
    import argparse

    parser = argparse.ArgumentParser(description="同步翻译文件")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--langs", nargs="+")
    parser.add_argument(
        "--interfaces",
        nargs="+",
        metavar="PATH",
        help="interface 文件或目录（支持多个）"
    )

    args = parser.parse_args()

    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    # ========= 修改点 =========
    if args.interfaces:
        interface_inputs = [Path(p) for p in args.interfaces]
    else:
        interface_inputs = [
            project_root / "assets" / "interface.json",
            project_root / "assets" / "tasks"
        ]

    interface_paths = resolve_interface_paths(interface_inputs)

    print("加载的 interface 文件:")
    for p in interface_paths:
        print(f"  - {p}")
    print()

    lang_dir = project_root / "assets" / "lang"
    if not args.dry_run:
        lang_dir.mkdir(exist_ok=True)

    print("步骤 1: 提取 key")
    keys = extract_keys_from_interfaces(interface_paths)
    print(f"✓ 共 {len(keys)} 个\n")

    print("步骤 2: 同步 zh-CN")
    zh_cn_path = lang_dir / "zh-CN.json"
    zh_cn = sync_zh_cn(keys, zh_cn_path, args.dry_run)

    print("步骤 3: 同步其他语言")
    translate_to_other_langs(zh_cn, lang_dir, args.langs, args.dry_run)

    print("\n✓ 完成")


if __name__ == "__main__":
    main()