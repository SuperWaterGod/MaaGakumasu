#!/usr/bin/env python3
"""
翻译文件同步脚本 v2
自动从 interface.json 提取所有需要翻译的 key，并同步到翻译文件

使用方式:
    python tools/sync_lang.py [--dry-run] [--langs LANG ...]

功能:
1. 从 interface.json 递归提取所有 label 和 doc 字段中 $ 开头的字符串
2. 同步到 zh-CN.json（按 interface.json 出现顺序排序）
3. 自动翻译到其他语言文件（如使用 OpenCC 转换繁体）
4. 生成同步报告
"""

import json
from pathlib import Path
from collections import OrderedDict

try:
    from opencc import OpenCC

    HAS_OPENCC = True
except ImportError:
    HAS_OPENCC = False


def _extract_dollar_keys_ordered(value: any, keys: list) -> None:
    """递归提取 $ 开头的字符串（保持顺序）"""
    if isinstance(value, str):
        if value.startswith("$"):
            key = value[1:]
            if key not in keys:
                keys.append(key)
    elif isinstance(value, list):
        for item in value:
            _extract_dollar_keys_ordered(item, keys)
    elif isinstance(value, dict):
        # 优先处理 label 和 doc 字段
        if "label" in value:
            _extract_dollar_keys_ordered(value["label"], keys)
        if "doc" in value:
            _extract_doc_key_ordered(value["doc"], keys)
        # 然后处理其他字段
        for k, v in value.items():
            if k not in ["label", "doc"]:
                _extract_dollar_keys_ordered(v, keys)


def _extract_doc_key_ordered(doc: any, keys: list) -> None:
    """提取 doc 字段的翻译 key（保持顺序）"""
    if isinstance(doc, list):
        # 数组形式的 doc：检查每个元素，只有全部以 $ 开头才处理
        cleaned_items = []
        for item in doc:
            if isinstance(item, str) and item.startswith("$"):
                cleaned_items.append(item[1:])
            elif isinstance(item, str) and item:
                # 有非 $ 开头的非空字符串，不处理整个数组
                return

        if cleaned_items:
            merged_key = "\n".join(cleaned_items)
            if merged_key not in keys:
                keys.append(merged_key)
    elif isinstance(doc, str) and doc.startswith("$"):
        key = doc[1:]
        if key not in keys:
            keys.append(key)


def extract_keys_from_interface(interface_path: Path) -> list:
    """从 interface.json 提取所有需要翻译的 key（保持顺序）"""
    with open(interface_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    keys = []
    _extract_dollar_keys_ordered(data, keys)
    return keys


def _load_translations(lang_path: Path) -> dict:
    """读取现有翻译文件"""
    if lang_path.exists():
        with open(lang_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _save_translations(translations: dict, lang_path: Path) -> None:
    """写入翻译文件（保持顺序）"""
    with open(lang_path, "w", encoding="utf-8") as f:
        json.dump(translations, f, ensure_ascii=False, indent=4)


def _print_key_preview(keys: list, prefix: str, max_show: int = 10) -> None:
    """预览显示 key 列表"""
    for key in keys[:max_show]:
        preview = key[:50] + "..." if len(key) > 50 else key
        print(f"  {prefix} {preview}")
    if len(keys) > max_show:
        print(f"  ... 还有 {len(keys) - max_show} 个")
    print()


def sync_zh_cn(required_keys: list, zh_cn_path: Path, dry_run: bool = False) -> dict:
    """同步简体中文翻译文件（按 interface.json 顺序）"""
    existing_translations = _load_translations(zh_cn_path)

    # 构建新的有序字典
    new_translations = OrderedDict()
    added_keys = []

    for key in required_keys:
        if key in existing_translations:
            new_translations[key] = existing_translations[key]
        else:
            new_translations[key] = key
            added_keys.append(key)

    # 检查删除的 key
    existing_keys = set(existing_translations.keys())
    required_keys_set = set(required_keys)
    removed_keys = existing_keys - required_keys_set

    print("=== zh-CN.json 同步报告 ===")
    print(f"需要的 key 数量: {len(required_keys)}")
    print(f"现有的 key 数量: {len(existing_keys)}")
    print(f"新增的 key 数量: {len(added_keys)}")
    print(f"移除的 key 数量: {len(removed_keys)}")
    print()

    if added_keys:
        print("--- 新增的 key ---")
        _print_key_preview(added_keys, "+")

    if removed_keys:
        print("--- 移除的 key ---")
        _print_key_preview(sorted(removed_keys), "-")

    if not dry_run:
        _save_translations(new_translations, zh_cn_path)
        print(f"✓ 已更新 {zh_cn_path.name}")
    else:
        print("(Dry run 模式，未修改文件)")

    print()
    return new_translations


def get_all_lang_configs():
    """获取所有支持的语言配置"""
    return {
        "zh-Hant": {
            "name": "繁体中文（台湾）",
            "converter": "s2twp" if HAS_OPENCC else None,
        },
        "en": {
            "name": "英文",
            "converter": None,
        },
        "ja": {
            "name": "日文",
            "converter": None,
        },
    }


def translate_to_other_langs(zh_cn_translations: dict, lang_dir: Path, target_langs: list = None, dry_run: bool = False):
    """将 zh-CN.json 自动翻译到其他语言文件"""

    all_lang_configs = get_all_lang_configs()

    if target_langs is None:
        target_langs = ["zh-Hant", "en", "ja"]

    # 验证目标语言
    lang_configs = {}
    for lang in target_langs:
        if lang in all_lang_configs:
            lang_configs[lang] = all_lang_configs[lang]
        else:
            print(f"⚠ 警告: 不支持的语言代码 '{lang}'，已跳过")

    if not lang_configs:
        print("⚠ 没有有效的目标语言，跳过翻译步骤")
        return

    for lang_code, config in lang_configs.items():
        lang_path = lang_dir / f"{lang_code}.json"

        # 使用转换器（如果可用）
        converter = None
        if config["converter"] and HAS_OPENCC:
            converter = OpenCC(config["converter"])

        # 步骤 1: 翻译所有简体内容（在内存中）
        auto_translations = OrderedDict()
        for key, zh_cn_value in zh_cn_translations.items():
            if converter:
                auto_translations[key] = converter.convert(zh_cn_value)
            else:
                auto_translations[key] = zh_cn_value

        # 步骤 2: 加载现有翻译文件
        existing_translations = _load_translations(lang_path)

        # 步骤 3: 对比并决策
        added_count = 0
        updated_count = 0
        kept_count = 0

        final_translations = OrderedDict()

        for key, auto_value in auto_translations.items():
            if key in existing_translations:
                existing_value = existing_translations[key]
                if auto_value != existing_value:
                    # 自动翻译结果不同，更新
                    final_translations[key] = auto_value
                    updated_count += 1
                else:
                    # 自动翻译结果相同，保留
                    final_translations[key] = existing_value
                    kept_count += 1
            else:
                # 新增的 key
                final_translations[key] = auto_value
                added_count += 1

        # 检查删除的 key
        removed_count = len(set(existing_translations.keys()) - set(zh_cn_translations.keys()))

        print(f"=== {config['name']} ({lang_code}.json) 同步报告 ===")
        print(f"新增: {added_count} 个 key")
        print(f"更新: {updated_count} 个 key（自动翻译结果不同）")
        print(f"保留: {kept_count} 个 key（自动翻译结果相同）")
        print(f"移除: {removed_count} 个 key")

        if converter:
            print(f"✓ 使用 OpenCC ({config['converter']}) 自动转换")
        elif config["converter"] and not HAS_OPENCC:
            print(f"⚠ 未安装 opencc-python-reimplemented，使用简体作为占位符")
        else:
            print(f"ℹ 使用简体中文作为占位符，需手动翻译")

        # 步骤 4: 写入覆盖
        if not dry_run:
            _save_translations(final_translations, lang_path)
            print(f"✓ 已更新 {lang_path.name}")
        else:
            print("(Dry run 模式，未修改文件)")

        print()


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="同步翻译文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
支持的语言代码:
  zh-Hant    繁体中文（台湾）- 使用 OpenCC 自动转换
  en         英文
  ja         日文

示例:
  python sync_lang.py                           # 同步默认语言 (zh-Hant, en, ja)
  python sync_lang.py --langs zh-Hant en        # 只同步繁体中文和英文
  python sync_lang.py --langs all               # 同步所有支持的语言
  python sync_lang.py --dry-run                 # 预览变更，不修改文件
        """
    )
    parser.add_argument("--dry-run", action="store_true", help="只显示差异，不修改文件")
    parser.add_argument(
        "--langs",
        nargs="+",
        metavar="LANG",
        help="指定要同步的语言代码，如 zh-Hant en ja，或使用 'all' 同步所有语言"
    )
    args = parser.parse_args()

    # 处理语言参数
    target_langs = None
    if args.langs:
        if "all" in args.langs:
            target_langs = list(get_all_lang_configs().keys())
            print(f"ℹ 将同步所有支持的语言: {', '.join(target_langs)}")
        else:
            target_langs = args.langs
            print(f"ℹ 将同步指定的语言: {', '.join(target_langs)}")
    else:
        print(f"ℹ 使用默认语言: zh-Hant, en, ja")
    print()

    # 路径设定
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    interface_path = project_root / "assets" / "interface.json"
    lang_dir = project_root / "assets" / "lang"

    # 确保 lang 目录存在
    if not args.dry_run:
        lang_dir.mkdir(exist_ok=True)

    print("=" * 60)
    print("翻译文件同步工具 v2")
    print("=" * 60)
    print()

    # 步骤 1: 提取翻译键
    print("步骤 1: 从 interface.json 提取翻译键...")
    required_keys = extract_keys_from_interface(interface_path)
    print(f"✓ 提取到 {len(required_keys)} 个翻译键")
    print()

    # 步骤 2: 同步到 zh-CN.json
    print("步骤 2: 同步到 zh-CN.json（按 interface.json 出现顺序）...")
    zh_cn_path = lang_dir / "zh-CN.json"
    zh_cn_translations = sync_zh_cn(required_keys, zh_cn_path, dry_run=args.dry_run)

    # 步骤 3: 自动翻译到其他语言
    print("步骤 3: 自动翻译到其他语言文件...")
    translate_to_other_langs(zh_cn_translations, lang_dir, target_langs=target_langs, dry_run=args.dry_run)

    # 步骤 4: 总结报告
    print("=" * 60)
    print("同步完成！")
    print("=" * 60)

    if not HAS_OPENCC:
        print()
        print("💡 提示: 安装 opencc-python-reimplemented 可自动转换繁体中文")
        print("   pip install opencc-python-reimplemented")


if __name__ == "__main__":
    main()