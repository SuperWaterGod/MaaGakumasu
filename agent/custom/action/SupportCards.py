import json
import time
import base64
from utils import logger
from difflib import SequenceMatcher

from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("SupportCardsAuto")
class SupportCardsAuto(CustomAction):
    """
    自动识别支持卡牌列表
    遍历3列4行的卡牌网格，识别每张卡牌的右上角文字
    识别完一页后向下滑动到下一页，直到所有卡片识别完成
    """

    GRID_COLS = 3
    GRID_ROWS = 4
    CARDS_PER_PAGE = GRID_COLS * GRID_ROWS

    GRID_ROI = [33, 420, 210, 80]
    CARD_WIDTH = 222
    CARD_HEIGHT = 128
    ACTION_DELAY = 0.5

    # 支持卡牌数据文件路径
    SUPPORT_CARDS_FILE = "assets/data/support_cards.json"
    # 相似度阈值
    SIMILARITY_THRESHOLD = 0.7

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        logger.success("事件: 识别支持卡牌")

        # 加载支持卡牌数据
        card_data = self.load_card_data()
        if not card_data:
            logger.error("加载卡牌数据失败")
            return False

        all_cards = []
        seen_names = set()
        page_index = 0

        while True:
            image = context.tasker.controller.post_screencap().wait().get()
            page_cards, should_stop = self._recognize_page_cards(context, image, page_index, seen_names, card_data)

            if should_stop:
                all_cards.extend(page_cards)
                break

            if not page_cards:
                logger.info("当前页无新卡牌，停止识别")
                break

            all_cards.extend(page_cards)
            logger.info(f"第{page_index + 1}页识别到 {len(page_cards)} 张卡牌")

            page_index += 1
            if not self._swipe_to_next_page(context):
                logger.info("滑动失败或已到最后一页")
                break

            time.sleep(self.ACTION_DELAY)

        logger.success(f"识别完成，共 {len(all_cards)} 张卡牌")

        # 输出处理后的ID
        matched_ids = [card["matched_id"] for card in all_cards if card.get("matched_id")]
        logger.info(f"识别结果: {','.join(matched_ids)}")

        # 检查匹配失败的卡牌
        unmatched = [card for card in all_cards if not card.get("matched_id")]
        if unmatched:
            logger.warning(f"有 {len(unmatched)} 张卡牌匹配失败:")
            for card in unmatched:
                logger.warning(f"  - {card['name']}, star={card['star']}")

        # 保存匹配成功的卡牌ID到文件（排除1-开头的卡牌）
        if matched_ids:
            filtered_ids = [mid for mid in matched_ids if not mid.startswith("1-")]
            content = ",".join(filtered_ids)
            encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
            with open("识别结果.txt", "w", encoding="utf-8") as f:
                f.write(encoded)
            logger.success("已保存Base64识别结果至 识别结果.txt (已排除R级卡牌)")

        return True

    def load_card_data(self) -> dict:
        """加载支持卡牌数据"""
        try:
            with open(self.SUPPORT_CARDS_FILE, "r", encoding="utf-8") as f:
                cards = json.load(f)
            # 构建名称到ID的映射
            return {card["name"]: card["id"] for card in cards}
        except Exception as e:
            logger.error(f"加载卡牌数据失败: {e}")
            return {}

    def match_card(self, name: str, star: int, card_data: dict) -> str | None:
        """匹配单张卡牌并处理ID"""
        # 精确匹配
        if name in card_data:
            return self.process_card_id(card_data[name], star)

        # 模糊匹配
        best_similarity = 0
        best_card_id = None
        best_card_name = None
        for card_name, card_id in card_data.items():
            similarity = self.calculate_similarity(name, card_name)
            if similarity > best_similarity:
                best_similarity = similarity
                best_card_id = card_id
                best_card_name = card_name

        if best_similarity >= self.SIMILARITY_THRESHOLD:
            logger.info(f"模糊匹配: '{name}' -> '{best_card_name}' (相似度: {best_similarity:.2f})")
            return self.process_card_id(best_card_id, star)

        return None

    def calculate_similarity(self, name1: str, name2: str) -> float:
        """计算两个字符串的相似度"""
        return SequenceMatcher(None, name1, name2).ratio()

    def process_card_id(self, card_id: str, star: int) -> str:
        """处理卡牌ID，格式: s_card-X-XXXX -> X-XX-star"""
        # s_card-1-0004 -> 1-4-1
        # s_card-3-0029 -> 3-29-star
        try:
            parts = card_id.split("-")
            if len(parts) >= 3:
                series = parts[1]
                index = str(int(parts[2]))
                return f"{series}-{index}-{star}"
        except Exception:
            pass
        return card_id

    def _recognize_page_cards(self, context: Context, image, page_index: int, seen_names: set, card_data: dict) -> tuple:
        """识别当前页的所有卡牌，返回 (卡牌列表, 是否停止)"""
        page_cards = []
        page_names = []  # 先收集本页所有卡牌名称
        should_stop = False

        # 先识别所有卡牌名称（不检查重复）
        for row in range(self.GRID_ROWS):
            for col in range(self.GRID_COLS):
                if context.tasker.stopping:
                    logger.info("任务中断")
                    return page_cards, False

                card_x = self.GRID_ROI[0] + col * self.CARD_WIDTH + self.CARD_WIDTH // 2
                card_y = self.GRID_ROI[1] + row * self.CARD_HEIGHT + 100

                if page_index == 0 and row == 0 and col == 0:
                    pass
                else:
                    context.tasker.controller.post_click(card_x, card_y).wait()
                    time.sleep(self.ACTION_DELAY)

                card_name = self._recognize_card_name(context)
                star_count = self._recognize_star_count(context)

                if card_name:
                    page_names.append((card_name, star_count))
                    logger.info(f"已识别: {card_name}")

        # 本页识别完成后检查重复，继续处理所有卡牌
        for card_name, star_count in page_names:
            if card_name in seen_names:
                should_stop = True
            else:
                seen_names.add(card_name)
                matched_id = self.match_card(card_name, star_count, card_data)
                page_cards.append({
                    "name": card_name,
                    "star": star_count,
                    "matched_id": matched_id,
                    "page": page_index + 1,
                })

        return page_cards, should_stop

    def _recognize_card_name(self, context: Context) -> str:
        """识别卡牌右上角的文字"""
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("SupportCardsOCR", image)
        if reco_detail and reco_detail.hit:
            text = "".join(item.text for item in reco_detail.filtered_results)
            return text.strip()
        return ""

    def _recognize_star_count(self, context: Context) -> int:
        """识别卡牌star数量"""
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("SupportCardsStar", image)
        if reco_detail and reco_detail.hit:
            return len(reco_detail.filtered_results)
        return 0

    def _swipe_to_next_page(self, context: Context) -> bool:
        """滑动到下一页"""
        context.tasker.controller.post_swipe(247, 920, 247, 485, 1500).wait()
        return True