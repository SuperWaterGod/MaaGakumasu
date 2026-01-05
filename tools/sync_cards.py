import json
from datetime import datetime
from collections import defaultdict


def format_cards_data(cards_data_path, output_path, card_types=None):
    """
    将cards_data.json格式化为interface.json格式
    
    参数:
    - cards_data_path: cards_data.json文件路径
    - output_path: 输出文件路径
    - card_types: 要包含的卡片类型列表，如['SSR', 'SR', 'R']，None表示全部
    """
    if card_types is None:
        card_types = ['SSR', 'SR', 'R']

    # 读取cards_data.json
    with open(cards_data_path, 'r', encoding='utf-8') as f:
        cards_data = json.load(f)

    # 按偶像名称分组卡片
    idol_cards = defaultdict(list)

    for card_type in card_types:
        if card_type in cards_data:
            for card in cards_data[card_type]:
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

    # 为每个偶像生成interface格式
    new_interface_data = {}

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

        # 生成cases
        cases = []
        for card in unique_cards:
            cases.append({
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

        # 选择最新的卡片作为default_case
        default_case = unique_cards[0]['card_name'] if unique_cards else ""

        # 构建该偶像的配置
        new_interface_data[key_name] = {
            "type": "select",
            "default_case": default_case,
            "cases": cases,
            "label": f"${key_name}"
        }

    # 保存到输出文件
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(new_interface_data, f, ensure_ascii=False, indent='\t')

    print(f"\n处理完成！已生成 {len(new_interface_data)} 个偶像的配置")
    print(f"输出文件: {output_path}")

    # 显示统计信息
    for key_name, config in new_interface_data.items():
        print(f"\n{key_name}:")
        print(f"  默认卡片: {config['default_case']}")
        print(f"  卡片数量: {len(config['cases'])}")


if __name__ == "__main__":

    format_cards_data(
        cards_data_path='cards_data.json',
        output_path='interface.json',
        card_types=['SSR']
    )
