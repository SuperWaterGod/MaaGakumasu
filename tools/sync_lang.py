#!/usr/bin/env python3
"""
翻譯檔案同步腳本
自動從 interface.json 提取所有需要翻譯的 key，並同步到翻譯檔案

使用方式:
    python tools/sync_lang.py [--lang LANG_CODE]

功能:
1. 從 interface.json 提取所有 $ 開頭的翻譯 key
2. 檢查翻譯檔案中的 key：
   - 存在：跳過
   - 不存在：新增（預設值為簡體中文，即 key 本身）
   - 多餘：移除
"""

import json
from pathlib import Path

try:
    from opencc import OpenCC

    HAS_OPENCC = True
except ImportError:
    HAS_OPENCC = False


def _extract_dollar_keys(value: any, keys: set) -> None:
    """遞迴提取 $ 開頭的字串"""
    if isinstance(value, str):
        if value.startswith("$"):
            keys.add(value[1:])
    elif isinstance(value, list):
        for item in value:
            _extract_dollar_keys(item, keys)
    elif isinstance(value, dict):
        for v in value.values():
            _extract_dollar_keys(v, keys)


def _extract_task_keys(tasks: list, keys: set) -> None:
    """提取 task 的 label 和 doc"""
    for task in tasks:
        if task.get("label"):
            _extract_dollar_keys(task["label"], keys)
        if task.get("doc"):
            _extract_doc_key(task["doc"], keys)


def _extract_doc_key(doc: any, keys: set) -> None:
    """提取 doc 欄位的翻譯 key"""
    if isinstance(doc, list):
        # 陣列形式的 doc 會被 MFAAvalonia 合併成一個字串再翻譯
        keys.add("\n".join(doc))
    elif isinstance(doc, str):
        keys.add(doc[1:] if doc.startswith("$") else doc)


def _extract_option_keys(options: dict, keys: set) -> None:
    """提取 option 的 label 和 cases"""
    for opt_data in options.values():
        if opt_data.get("label"):
            _extract_dollar_keys(opt_data["label"], keys)
        for case in opt_data.get("cases", []):
            if case.get("name"):
                keys.add(case["name"])


def extract_keys_from_interface(interface_path: Path) -> set:
    """從 interface.json 提取所有需要翻譯的 key"""
    with open(interface_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    keys = set()
    _extract_task_keys(data.get("task", []), keys)
    _extract_option_keys(data.get("option", {}), keys)
    return keys


def _load_translations(lang_path: Path) -> dict:
    """讀取現有翻譯檔案"""
    if lang_path.exists():
        with open(lang_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _print_key_preview(keys: set, prefix: str, max_show: int = 10) -> None:
    """預覽顯示 key 列表"""
    for key in sorted(keys)[:max_show]:
        preview = key[:50] + "..." if len(key) > 50 else key
        print(f"  {prefix} {preview}")
    if len(keys) > max_show:
        print(f"  ... 還有 {len(keys) - max_show} 個")
    print()


def _print_sync_report(
    required_keys: set, existing_keys: set, missing_keys: set, extra_keys: set
) -> None:
    """輸出同步報告"""
    print(f"=== 翻譯檔案同步報告 ===")
    print(f"需要的 key 數量: {len(required_keys)}")
    print(f"現有的 key 數量: {len(existing_keys)}")
    print(f"缺少的 key 數量: {len(missing_keys)}")
    print(f"多餘的 key 數量: {len(extra_keys)}")
    print()

    if missing_keys:
        print("--- 缺少的 key（將新增）---")
        _print_key_preview(missing_keys, "+")

    if extra_keys:
        print("--- 多餘的 key（將移除）---")
        _print_key_preview(extra_keys, "-")


def _add_missing_keys(translations: dict, missing_keys: set, lang_code: str) -> None:
    """新增缺少的 key 到翻譯字典"""
    if lang_code == "zh-Hant" and HAS_OPENCC:
        cc = OpenCC("s2twp")  # 簡體 -> 繁體（台灣用詞）
        for key in missing_keys:
            translations[key] = cc.convert(key)
        print(f"  使用 OpenCC 自動轉換新增的 key")
    else:
        for key in missing_keys:
            translations[key] = key  # 預設值 = key 本身（簡體中文）
        if lang_code == "zh-Hant" and not HAS_OPENCC:
            print(f"  提示: 安裝 opencc-python-reimplemented 可自動轉換繁體")


def _save_translations(translations: dict, lang_path: Path) -> None:
    """按 key 排序後寫入翻譯檔案"""
    sorted_translations = dict(sorted(translations.items()))
    with open(lang_path, "w", encoding="utf-8") as f:
        json.dump(sorted_translations, f, ensure_ascii=False, indent=4)


def sync_lang_file(
    interface_path: Path, lang_path: Path, lang_code: str, dry_run: bool = False
):
    """同步翻譯檔案"""
    required_keys = extract_keys_from_interface(interface_path)
    translations = _load_translations(lang_path)
    existing_keys = set(translations.keys())

    missing_keys = required_keys - existing_keys
    extra_keys = existing_keys - required_keys

    _print_sync_report(required_keys, existing_keys, missing_keys, extra_keys)

    if dry_run:
        print("(Dry run 模式，不會修改檔案)")
        return

    _add_missing_keys(translations, missing_keys, lang_code)

    for key in extra_keys:
        del translations[key]

    _save_translations(translations, lang_path)

    print(f"已更新 {lang_path}")
    print(f"  新增: {len(missing_keys)} 個 key")
    print(f"  移除: {len(extra_keys)} 個 key")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="同步翻譯檔案")
    parser.add_argument("--dry-run", action="store_true", help="只顯示差異，不修改檔案")
    parser.add_argument(
        "--lang",
        default="zh-Hant",
        help="語言代碼，例如 zh-Hant、ja、en（預設：zh-Hant）",
    )
    args = parser.parse_args()

    # 路徑設定
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    interface_path = project_root / "assets" / "interface.json"
    lang_dir = project_root / "assets" / "lang"

    # 確保 lang 目錄存在
    lang_dir.mkdir(exist_ok=True)

    # 同步指定語言檔案
    lang_path = lang_dir / f"{args.lang}.json"
    print(f"目標語言: {args.lang}")
    sync_lang_file(interface_path, lang_path, args.lang, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
