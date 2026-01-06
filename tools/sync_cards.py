import json
from datetime import datetime
from collections import defaultdict, OrderedDict


def safe_print(text):
    """安全打印，处理编码问题"""
    try:
        print(text)
    except UnicodeEncodeError:
        # 如果打印失败，尝试用ASCII安全字符替换
        print(text.encode('gbk', errors='replace').decode('gbk'))


def format_cards_data(idols_cards_path, interface_path, output_path, card_types=None):
    """
    增量更新interface.json中的option配置

    参数:
    - idols_cards_path: idols_cards.json文件路径
    - interface_path: 现有的interface.json文件路径
    - output_path: 输出文件路径
    - card_types: 要包含的卡片类型列表，如['SSR', 'SR', 'R']，None表示全部
    """
    if card_types is None:
        card_types = ['SSR', 'SR', 'R']

    # 读取idols_cards.json
    with open(idols_cards_path, 'r', encoding='utf-8') as f:
        idols_cards_data = json.load(f)

    # 读取现有的interface.json
    try:
        with open(interface_path, 'r', encoding='utf-8') as f:
            interface_data = json.load(f)
    except FileNotFoundError:
        safe_print(f"警告: 未找到 {interface_path}，将创建新文件")
        interface_data = {}

    # 确保option字段存在
    if 'option' not in interface_data:
        interface_data['option'] = {}

    # 按偶像名称分组卡片
    idol_cards = defaultdict(list)

    for card_type in card_types:
        if card_type in idols_cards_data:
            for card in idols_cards_data[card_type]:
                idol_name = card['偶像名称']
                card_name = card['卡片名称']
                song_name = card['歌曲名称']
                date_str = card['登场日期']

                # 解析日期
                try:
                    card_date = datetime.strptime(date_str, '%Y/%m/%d')
                except:
                    card_date = datetime.min

                idol_cards[idol_name].append({
                    'card_name': card_name,
                    'idol_name': idol_name,
                    'song_name': song_name,
                    'date': card_date
                })

    # 记录新增的卡片
    new_cards_log = defaultdict(list)
    updated_idols = []

    # 为每个偶像生成或更新配置
    for idol_name, cards in idol_cards.items():
        key_name = f"{idol_name}卡片"

        # 按日期排序，最新的在前
        cards_sorted = sorted(cards, key=lambda x: x['date'], reverse=True)

        # 去重
        seen_cards = set()
        unique_cards = []
        for card in cards_sorted:
            if card['card_name'] not in seen_cards:
                seen_cards.add(card['card_name'])
                unique_cards.append(card)

        # 生成新的cases
        new_cases = []
        for card in unique_cards:
            new_cases.append({
                "name": card['card_name'],
                "pipeline_override": {
                    "ProduceChooseIdol": {
                        "custom_recognition_param": {
                            "idol_name": card['idol_name'],
                            "song_name": card['song_name']
                        }
                    }
                }
            })

        # 检查是否已存在该偶像的配置
        if key_name in interface_data['option']:
            # 已存在，进行增量更新
            existing_config = interface_data['option'][key_name]
            existing_cases = existing_config.get('cases', [])
            existing_card_names = {case['name'] for case in existing_cases}

            # 找出新增的卡片
            for case in new_cases:
                if case['name'] not in existing_card_names:
                    new_cards_log[idol_name].append(case['name'])

            # 更新cases（保持新卡片在前的顺序）
            interface_data['option'][key_name]['cases'] = new_cases

            # 更新default_case为最新的卡片
            if unique_cards:
                interface_data['option'][key_name]['default_case'] = unique_cards[0]['card_name']

            if new_cards_log[idol_name]:
                updated_idols.append(idol_name)
        else:
            # 不存在，新建配置
            default_case = unique_cards[0]['card_name'] if unique_cards else ""

            interface_data['option'][key_name] = {
                "type": "select",
                "default_case": default_case,
                "cases": new_cases,
                "label": f"${key_name}"
            }

            # 记录所有卡片为新增
            new_cards_log[idol_name] = [card['card_name'] for card in unique_cards]
            updated_idols.append(idol_name)

    # 保存到输出文件
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(interface_data, f, ensure_ascii=False, indent=2)

    # 打印结果
    safe_print(f"\n{'=' * 60}")
    safe_print(f"处理完成！")
    safe_print(f"输出文件: {output_path}")
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
        safe_print("\n没有新增或更新的卡片")

    # 显示总体统计
    safe_print(f"\n{'=' * 60}")
    safe_print(f"总体统计：")

    # 只统计以"卡片"结尾的选项（偶像卡片配置）
    idol_card_options = {k: v for k, v in interface_data['option'].items() if k.endswith('卡片')}

    safe_print(f"  偶像总数: {len(idol_card_options)} 位")
    total_cards = sum(len(config['cases']) for config in idol_card_options.values())
    safe_print(f"  卡片总数: {total_cards} 张")
    total_new_cards = sum(len(cards) for cards in new_cards_log.values())
    safe_print(f"  本次新增: {total_new_cards} 张")
    safe_print(f"{'=' * 60}\n")


if __name__ == "__main__":
    format_cards_data(
        idols_cards_path='../assets/data/idols_cards.json',
        interface_path='../assets/interface.json',
        output_path='../assets/interface.json',
        card_types=['SSR', 'SR', 'R']
    )