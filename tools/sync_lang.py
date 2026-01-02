#!/usr/bin/env python3
"""
翻譯檔案同步腳本
自動從 interface.json 提取所有需要翻譯的 key，並同步到翻譯檔案

使用方式:
    python tools/sync_lang.py

功能:
1. 從 interface.json 提取所有 $ 開頭的翻譯 key
2. 檢查翻譯檔案中的 key：
   - 存在：跳過
   - 不存在：新增（預設值為簡體中文，即 key 本身）
   - 多餘：移除
"""

import json
import re
from pathlib import Path

try:
    from opencc import OpenCC

    HAS_OPENCC = True
except ImportError:
    HAS_OPENCC = False


def extract_keys_from_interface(interface_path: Path) -> set:
    """從 interface.json 提取所有需要翻譯的 key"""
    with open(interface_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    keys = set()

    def extract_from_value(value):
        """遞迴提取 $ 開頭的字串"""
        if isinstance(value, str):
            if value.startswith("$"):
                # 去掉 $ 前綴作為 key
                keys.add(value[1:])
        elif isinstance(value, list):
            for item in value:
                extract_from_value(item)
        elif isinstance(value, dict):
            for v in value.values():
                extract_from_value(v)

    # 提取 task 的 label 和 doc
    for task in data.get("task", []):
        if task.get("label"):
            extract_from_value(task["label"])
        if task.get("doc"):
            doc = task["doc"]
            if isinstance(doc, list):
                # 陣列形式的 doc 會被 MFAAvalonia 合併成一個字串再翻譯
                combined = "\n".join(doc)
                keys.add(combined)
            elif isinstance(doc, str):
                if doc.startswith("$"):
                    keys.add(doc[1:])
                else:
                    keys.add(doc)

    # 提取 option 的 label 和 cases
    for opt_name, opt_data in data.get("option", {}).items():
        if opt_data.get("label"):
            extract_from_value(opt_data["label"])

        # 提取 cases 的 name
        for case in opt_data.get("cases", []):
            if case.get("name"):
                # case name 本身作為 key（不需要 $ 前綴）
                keys.add(case["name"])

    return keys


def sync_lang_file(interface_path: Path, lang_path: Path, dry_run: bool = False):
    """同步翻譯檔案"""
    # 提取所有 key
    required_keys = extract_keys_from_interface(interface_path)

    # 讀取現有翻譯
    if lang_path.exists():
        with open(lang_path, "r", encoding="utf-8") as f:
            translations = json.load(f)
    else:
        translations = {}

    existing_keys = set(translations.keys())

    # 計算差異
    missing_keys = required_keys - existing_keys
    extra_keys = existing_keys - required_keys

    # 統計
    print(f"=== 翻譯檔案同步報告 ===")
    print(f"需要的 key 數量: {len(required_keys)}")
    print(f"現有的 key 數量: {len(existing_keys)}")
    print(f"缺少的 key 數量: {len(missing_keys)}")
    print(f"多餘的 key 數量: {len(extra_keys)}")
    print()

    # 顯示缺少的 key
    if missing_keys:
        print("--- 缺少的 key（將新增）---")
        for key in sorted(missing_keys)[:10]:
            preview = key[:50] + "..." if len(key) > 50 else key
            print(f"  + {preview}")
        if len(missing_keys) > 10:
            print(f"  ... 還有 {len(missing_keys) - 10} 個")
        print()

    # 顯示多餘的 key
    if extra_keys:
        print("--- 多餘的 key（將移除）---")
        for key in sorted(extra_keys)[:10]:
            preview = key[:50] + "..." if len(key) > 50 else key
            print(f"  - {preview}")
        if len(extra_keys) > 10:
            print(f"  ... 還有 {len(extra_keys) - 10} 個")
        print()

    if dry_run:
        print("(Dry run 模式，不會修改檔案)")
        return

    # 執行同步
    # 1. 新增缺少的 key（自動翻譯成繁體中文）
    if HAS_OPENCC:
        cc = OpenCC("s2twp")  # s2twp = 簡體到繁體(台灣用詞)
        for key in missing_keys:
            translations[key] = cc.convert(key)
        print(f"  使用 OpenCC 自動翻譯新增的 key")
    else:
        for key in missing_keys:
            translations[key] = key  # 預設值 = key 本身（簡體中文）
        print(f"  提示: 安裝 opencc-python-reimplemented 可自動翻譯")

    # 2. 移除多餘的 key
    for key in extra_keys:
        del translations[key]

    # 3. 按 key 排序後寫入
    sorted_translations = dict(sorted(translations.items()))

    with open(lang_path, "w", encoding="utf-8") as f:
        json.dump(sorted_translations, f, ensure_ascii=False, indent=4)

    print(f"已更新 {lang_path}")
    print(f"  新增: {len(missing_keys)} 個 key")
    print(f"  移除: {len(extra_keys)} 個 key")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="同步翻譯檔案")
    parser.add_argument("--dry-run", action="store_true", help="只顯示差異，不修改檔案")
    args = parser.parse_args()

    # 路徑設定
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    interface_path = project_root / "assets" / "interface.json"
    lang_dir = project_root / "assets" / "lang"

    # 確保 lang 目錄存在
    lang_dir.mkdir(exist_ok=True)

    # 同步 zh-Hant.json
    lang_path = lang_dir / "zh-Hant.json"
    sync_lang_file(interface_path, lang_path, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
