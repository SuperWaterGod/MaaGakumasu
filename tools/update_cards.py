import requests
from bs4 import BeautifulSoup
import json
import re
from datetime import datetime

# 推荐效果翻译映射
EFFECT_TRANSLATIONS = {
    'ｷロジックやる気': '理性·干劲',
    'ｶロジック好印象': '理性·好印象',
    'ｱセンス好調': '感性·好调',
    'ｲセンス集中': '感性·集中',
    'ｻアノマリー強気': '非凡·强气',
    'ｼアノマリー全力': '非凡·全力',
    'ｽアノマリー温存': '非凡·温存',
}


def safe_print(text):
    """安全打印，处理编码问题"""
    try:
        print(text)
    except UnicodeEncodeError:
        # 如果打印失败，尝试用ASCII安全字符替换
        print(text.encode('gbk', errors='replace').decode('gbk'))


def extract_card_info(card_name):
    """
    从卡片名称中提取偶像名称和歌曲名称
    例如: "【Campus mode!!】倉本千奈" -> ("倉本千奈", "Campus mode!!")
    """
    # 匹配【歌曲名】偶像名 的格式
    match = re.match(r'【(.+?)】(.+)', card_name)
    if match:
        song_name = match.group(1).strip()
        idol_name = match.group(2).strip()
        return idol_name, song_name
    return card_name, ""


def translate_effect(effect_text):
    """
    翻译推荐效果
    """
    # 移除特殊字符（如ｱｶｷ等全角字母）
    cleaned = effect_text.strip()

    # 尝试直接匹配
    if cleaned in EFFECT_TRANSLATIONS:
        return EFFECT_TRANSLATIONS[cleaned]

    # 处理可能的空格问题
    cleaned_no_space = cleaned.replace(' ', '').replace('　', '')
    for key, value in EFFECT_TRANSLATIONS.items():
        key_no_space = key.replace(' ', '').replace('　', '')
        if cleaned_no_space == key_no_space:
            return value

    # 如果没有匹配，返回原文
    return effect_text


def scrape_cards_from_url(url):
    """
    从URL采集网站中的SSR、SR和R卡片信息
    """
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers)
        response.encoding = 'EUC-JP'

        soup = BeautifulSoup(response.text, 'html.parser')

        all_cards = {
            'SSR': [],
            'SR': [],
            'R': []
        }

        # 统计プラン推荐效果
        plan_effects_stats = {}

        # 找到SSR部分
        ssr_section = soup.find('h4', id='content_1_1')
        if ssr_section:
            # 找到SSR表格
            parent = ssr_section.find_parent('div', class_='wiki-section-2')
            if parent:
                table = parent.find('table', {'class': ['sort', 'filter']})
                if table:
                    all_cards['SSR'] = parse_table(table, plan_effects_stats)
                    print(f"找到 {len(all_cards['SSR'])} 张SSR卡片")

        # 找到SR部分
        sr_section = soup.find('h4', id='content_1_2')
        if sr_section:
            parent = sr_section.find_parent('div', class_='wiki-section-2')
            if parent:
                table = parent.find('table', {'class': ['sort', 'filter']})
                if table:
                    all_cards['SR'] = parse_table(table, plan_effects_stats)
                    print(f"找到 {len(all_cards['SR'])} 张SR卡片")

        # 找到R部分
        r_section = soup.find('h4', id='content_1_3')
        if r_section:
            parent = r_section.find_parent('div', class_='wiki-section-2')
            if parent:
                table = parent.find('table', {'class': ['sort', 'filter']})
                if table:
                    all_cards['R'] = parse_table(table, plan_effects_stats)
                    print(f"找到 {len(all_cards['R'])} 张R卡片")

        return all_cards, plan_effects_stats

    except Exception as e:
        print(f"错误: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def parse_table(table, plan_effects_stats):
    """
    解析单个表格，返回卡片列表
    """
    cards = []

    # 获取表头
    thead = table.find('thead')
    headers = []
    if thead:
        header_row = thead.find('tr')
        if header_row:
            headers = [th.get_text(strip=True) for th in header_row.find_all('th')]

    # 获取数据行
    tbody = table.find('tbody')
    if tbody:
        rows = tbody.find_all('tr')
    else:
        rows = table.find_all('tr')[1:]  # 跳过表头

    for row in rows:
        cells = row.find_all(['td', 'th'])
        if len(cells) < 2:
            continue

        card_data = {}
        raw_card_name = ""
        raw_effect = ""

        for i, cell in enumerate(cells):
            text = cell.get_text(strip=True)

            # 使用表头作为键名
            if i < len(headers) and headers[i]:
                header = headers[i]

                # 卡片名称
                if header == 'カード名':
                    raw_card_name = text
                    idol_name, song_name = extract_card_info(text)
                    card_data['卡片名称'] = f"{idol_name}({song_name})" if song_name else idol_name
                    card_data['偶像名称'] = idol_name
                    card_data['歌曲名称'] = song_name
                # 将Vo、Da、Vi转换为数字
                elif header in ['Vo', 'Da', 'Vi']:
                    try:
                        card_data[header] = int(text) if text else 0
                    except ValueError:
                        card_data[header] = text
                # 统计プラン推荐效果
                elif 'プラン' in header or 'おすすめ' in header:
                    raw_effect = text
                    translated = translate_effect(text)
                    card_data['推荐效果'] = translated
                    if text:
                        plan_effects_stats[text] = plan_effects_stats.get(text, 0) + 1
                # 去掉"ボーナス"列中的百分号并转为浮点数
                elif header == 'ボーナス':
                    try:
                        # 处理百分比
                        cleaned = text.replace('%', '').strip()
                        card_data['奖励加成'] = float(cleaned) if cleaned else 0.0
                    except ValueError:
                        card_data['奖励加成'] = text
                # 体力
                elif header == '体力':
                    card_data['体力'] = text
                # 登场日期
                elif header == '登場日' or '登場' in header:
                    card_data['登场日期'] = text
                else:
                    # 其他字段保持原样
                    card_data[header] = text

        # 只添加有卡片名的记录
        if raw_card_name:
            cards.append(card_data)

    return cards


def parse_date(date_str):
    """
    将日期字符串转换为可比较的格式
    例如: "2025/01/09" -> (2025, 1, 9)
    """
    try:
        parts = date_str.split('/')
        if len(parts) == 3:
            return int(parts[0]), int(parts[1]), int(parts[2])
        return 0, 0, 0
    except:
        return 0, 0, 0


def sort_cards(cards):
    """
    对卡片进行排序：先按偶像名称倒序，再按登场日期从新到旧
    """
    return sorted(cards, key=lambda x: (
        # 偶像名称倒序
        x.get('偶像名称', ''),
        # 日期从新到旧，所以用负数排序
        tuple(v for v in parse_date(x.get('登场日期', '0/0/0')))
    ), reverse=True)


def load_old_data(filename):
    """
    加载旧的JSON数据
    """
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"读取旧文件时出错: {e}")
        return None


def create_card_key(card):
    """
    为卡片创建唯一键，用于比较
    """
    return f"{card.get('偶像名称', '')}|{card.get('歌曲名称', '')}"


def compare_data(old_data, new_data):
    """
    比较新旧数据的差异
    返回: (新增, 修改, 删除)
    """
    added = {'SSR': [], 'SR': [], 'R': []}
    modified = {'SSR': [], 'SR': [], 'R': []}
    deleted = {'SSR': [], 'SR': [], 'R': []}

    if old_data is None:
        # 如果没有旧数据，所有都是新增
        for rarity in ['SSR', 'SR', 'R']:
            added[rarity] = new_data[rarity].copy()
        return added, modified, deleted

    for rarity in ['SSR', 'SR', 'R']:
        # 创建旧数据和新数据的字典映射
        old_cards = {create_card_key(card): card for card in old_data.get(rarity, [])}
        new_cards = {create_card_key(card): card for card in new_data[rarity]}

        # 找出新增的卡片
        for key, card in new_cards.items():
            if key not in old_cards:
                added[rarity].append(card)

        # 找出删除的卡片
        for key, card in old_cards.items():
            if key not in new_cards:
                deleted[rarity].append(card)

        # 找出修改的卡片
        for key in set(old_cards.keys()) & set(new_cards.keys()):
            old_card = old_cards[key]
            new_card = new_cards[key]

            # 比较卡片的所有字段
            differences = []
            for field in ['体力', 'Vo', 'Da', 'Vi', '奖励加成', '推荐效果', '登场日期']:
                old_value = old_card.get(field)
                new_value = new_card.get(field)
                if old_value != new_value:
                    differences.append({
                        'field': field,
                        'old': old_value,
                        'new': new_value
                    })

            if differences:
                modified[rarity].append({
                    'card': new_card,
                    'changes': differences
                })

    return added, modified, deleted


def print_comparison_report(added, modified, deleted):
    """
    打印数据变化报告
    """
    has_changes = False

    safe_print("\n" + "=" * 60)
    safe_print("数据变化报告")
    safe_print("=" * 60)

    # 新增的卡片
    total_added = sum(len(added[r]) for r in ['SSR', 'SR', 'R'])
    if total_added > 0:
        has_changes = True
        safe_print(f"\n【新增卡片】共 {total_added} 张")
        for rarity in ['SSR', 'SR', 'R']:
            if added[rarity]:
                safe_print(f"\n  {rarity}:")
                for card in added[rarity]:
                    safe_print(f"    + {card['卡片名称']}")
                    safe_print(f"      偶像: {card['偶像名称']} | 歌曲: {card['歌曲名称']}")
                    safe_print(f"      登场日期: {card.get('登场日期', 'N/A')}")

    # 修改的卡片
    total_modified = sum(len(modified[r]) for r in ['SSR', 'SR', 'R'])
    if total_modified > 0:
        has_changes = True
        safe_print(f"\n【修改的卡片】共 {total_modified} 张")
        for rarity in ['SSR', 'SR', 'R']:
            if modified[rarity]:
                safe_print(f"\n  {rarity}:")
                for item in modified[rarity]:
                    card = item['card']
                    changes = item['changes']
                    safe_print(f"    ~ {card['卡片名称']}")
                    for change in changes:
                        safe_print(f"      {change['field']}: {change['old']} → {change['new']}")

    # 删除的卡片
    total_deleted = sum(len(deleted[r]) for r in ['SSR', 'SR', 'R'])
    if total_deleted > 0:
        has_changes = True
        safe_print(f"\n【删除的卡片】共 {total_deleted} 张")
        for rarity in ['SSR', 'SR', 'R']:
            if deleted[rarity]:
                safe_print(f"\n  {rarity}:")
                for card in deleted[rarity]:
                    safe_print(f"    - {card['卡片名称']}")
                    safe_print(f"      偶像: {card['偶像名称']} | 歌曲: {card['歌曲名称']}")

    if not has_changes:
        safe_print("\n   数据无变化")

    safe_print("\n" + "=" * 60)


def save_to_json(data, filename='cards_data.json'):
    """
    将数据保存到JSON文件（覆盖模式），并比较差异
    """
    try:
        # 加载旧数据
        old_data = load_old_data(filename)

        # 对每个稀有度的卡片进行排序
        sorted_data = {
            '保存时间': datetime.now().strftime('%Y/%m/%d'),
            'SSR': sort_cards(data['SSR']),
            'SR': sort_cards(data['SR']),
            'R': sort_cards(data['R'])
        }

        # 比较新旧数据
        if old_data:
            added, modified, deleted = compare_data(old_data, sorted_data)
            print_comparison_report(added, modified, deleted)
        else:
            print("\n首次创建文件，无旧数据可比较")

        # 使用'w'模式覆盖写入
        with open(filename, 'w', encoding='utf-8') as f:
            json.dump(sorted_data, f, ensure_ascii=False, indent=2)

        print(f"\n数据已成功保存到 {filename} (覆盖模式)")
        print(f"保存时间: {sorted_data['保存时间']}")
        print(f"SSR卡片数量: {len(sorted_data['SSR'])}")
        print(f"SR卡片数量: {len(sorted_data['SR'])}")
        print(f"R卡片数量: {len(sorted_data['R'])}")
        print(f"总计: {len(sorted_data['SSR']) + len(sorted_data['SR']) + len(sorted_data['R'])} 张卡片")
        print(f"\n排序规则: 先按偶像名称倒序，再按登场日期从新到旧")
    except Exception as e:
        print(f"保存文件时出错: {e}")
        import traceback
        traceback.print_exc()


def main():
    url = "https://seesaawiki.jp/gakumasu/d/%a5%d7%a5%ed%a5%c7%a5%e5%a1%bc%a5%b9%a5%a2%a5%a4%a5%c9%a5%eb%b0%ec%cd%f7"

    print("开始采集卡片信息...")

    cards_data, plan_effects_stats = scrape_cards_from_url(url)

    if cards_data:
        print("\n采集完成！")
        save_to_json(cards_data, filename='../assets/data/idols_cards.json')

    else:
        print("采集失败，请检查网络连接或URL是否正确")


if __name__ == "__main__":
    main()
