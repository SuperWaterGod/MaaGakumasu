import json
import time
from math import ceil
from utils import logger
from typing import Optional
from jsonc_parser.parser import JsoncParser

from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("ShoppingCoinAuto")
class ShoppingCoinAuto(CustomAction):

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:
        # 收集商品价格信息
        coins = self._collect_coin_data(context)

        # 计算有效页数
        max_page = self._calculate_max_pages(coins)

        # 执行购买逻辑
        self._process_purchases(context, coins, max_page)

        return True

    @staticmethod
    def _collect_coin_data(context: Context) -> list:
        """收集商品价格数据"""
        max_page = 3
        image = context.tasker.controller.post_screencap().wait().get()
        coins = []

        for i in range(max_page):
            for j in range(2):
                reco_detail = context.run_recognition(
                    "ShoppingCoins", image,
                    pipeline_override={"ShoppingCoins": {
                        "recognition": "OCR",
                        "expected": "^\\d{1,3}(,\\d{3})*$",
                        "roi": [342 + 204 * j, 26 + 58 * i, 150, 44]
                    }})
                coins.append(
                    int(reco_detail.best_result.text.replace(",", "")) if reco_detail else None
                )

        logger.info(coins)
        return coins

    @staticmethod
    def _calculate_max_pages(coins: list) -> int:
        """计算需要处理的最大页数"""
        # 从后往前计算连续的None个数
        count = 0
        for i in range(len(coins) - 1, -1, -1):
            if coins[i] is None:
                count += 1
            else:
                break

        return max(2, ceil(len(coins) / 2) - (count // 2))

    @staticmethod
    def _should_buy_item(coin_value) -> bool:
        """判断是否应该购买商品"""
        return coin_value is not None and coin_value >= 10

    @staticmethod
    def _get_roi_for_item(page_index: int, item_index: int, coins: list, max_page: int) -> Optional[list]:
        """获取商品对应的ROI区域"""
        coin_idx = item_index + page_index * 2

        if not ShoppingCoinAuto._should_buy_item(coins[coin_idx]):
            return None

        # 特殊情况：最后一页且只有一个商品
        is_last_page_single_item = (
                page_index == max_page - 1 and
                coins[-1] is None and
                (len(coins) - sum(1 for c in coins if c is None)) % 2 == 1 and
                max_page % 2 == 1 and
                item_index == 0
        )

        if is_last_page_single_item:
            # print(f"商品{coin_idx + 1} 购买下面的")
            return [3, 730, 720, 550]
        elif item_index == 0:
            # print(f"商品{coin_idx + 1} 购买上面的")
            return [0, 230, 720, 500]
        else:
            # print(f"商品{coin_idx + 1} 购买下面的")
            return [3, 730, 720, 550]

    @staticmethod
    def _purchase_item(context: Context, roi: list):
        """执行购买操作"""
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("ShoppingCoinButton", image,
                                              pipeline_override={"ShoppingCoinButton": {"roi": roi}})
        if reco_detail:
            box = reco_detail.best_result.box
            context.tasker.controller.post_click(box[0], box[1]).wait()
            time.sleep(0.5)
            logger.info("开始购买")
            context.run_task("ShoppingCoinBuy")

    def _process_purchases(self, context: Context, coins: list, max_page: int):
        """处理所有页面的购买逻辑"""
        for i in range(max_page):
            logger.info(f"第{i + 1}页")

            # 处理第一个商品（上方商品）
            roi = self._get_roi_for_item(i, 0, coins, max_page)
            if roi:
                self._purchase_item(context, roi)

            # 处理第二个商品（下方商品）
            coin_idx = 1 + i * 2
            if coin_idx < len(coins):
                roi = None
                if self._should_buy_item(coins[coin_idx]):
                    # print(f"商品{coin_idx + 1} 购买下面的")
                    roi = [3, 730, 720, 550]
                # else:
                # print(f"商品{coin_idx + 1} 不购买")

                if roi:
                    self._purchase_item(context, roi)

            # 翻页（除了最后一页）
            if i + 1 < max_page:
                logger.info("翻页")
                context.run_task("ShoppingCoinNext")
                time.sleep(0.5)


@AgentServer.custom_action("ShoppingExchangeMoneyAuto")
class ShoppingExchangeMoneyAuto(CustomAction):

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:

        try:
            data = JsoncParser.parse_file("config.jsonc")
            params = data["shopping"]["money"]
        except (FileNotFoundError, Exception) as e:
            logger.info(f"加载 JSONC 文件时出错: {e}")
            params = json.loads(argv.custom_action_param)

        wishlist = []
        for key, value in params.items():
            if value:
                wishlist.append((key, value))
        logger.info("购买金币物品")
        max_page = 2
        for i in range(max_page):
            logger.info(f"第{i + 1}页")
            time.sleep(2)
            for key, value in wishlist:
                if key == "recommend":
                    logger.info("购买推荐物品")
                    file_name = f"shopping_recommend.png"
                else:
                    logger.info(f"购买{key}")
                    file_name = f"items/{key}.png"

                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition(
                    "ShoppingExchangeMoneyRecognition", image, pipeline_override={
                        "ShoppingExchangeMoneyRecognition": {
                            "recognition": "TemplateMatch",
                            "template": file_name,
                            "roi": [30, 288, 660, 667],
                            "threshold": 0.95
                        }})

                if context.tasker.stopping:
                    logger.info("任务中断")
                    return True

                if reco_detail:
                    for result in reco_detail.filterd_results:
                        box = result.box
                        context.tasker.controller.post_click(box[0] + 70, box[1] + 70).wait()
                        time.sleep(0.5)

                        image = context.tasker.controller.post_screencap().wait().get()
                        reco_detail = context.run_recognition("ShoppingPlus", image)
                        if reco_detail:
                            box = reco_detail.best_result.box
                            context.tasker.controller.post_click(box[0], box[1]).wait()

                        context.run_task("ShoppingExchangeBuy")
                        time.sleep(0.5)
                        logger.info("购买成功")
                    time.sleep(2)
                else:
                    logger.info("未找到该物品")
            if i + 1 == max_page:
                break
            context.run_task("ShoppingCoinNext")
            time.sleep(1)
        logger.info("结束购买")
        return True


@AgentServer.custom_action("ShoppingExchangeAPAuto")
class ShoppingExchangeAPAuto(CustomAction):

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:

        try:
            data = JsoncParser.parse_file("config.jsonc")
            params = data["shopping"]["AP"]
        except (FileNotFoundError, Exception) as e:
            logger.info(f"加载 JSONC 文件时出错: {e}")
            params = json.loads(argv.custom_action_param)

        wishlist = []
        for key, value in params.items():
            if value:
                wishlist.append((key, value))

        logger.info("购买AP物品")
        for key, value in wishlist:
            logger.info(f"购买{key}")
            file_name = f"items/{key}.png"
            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition(
                "ShoppingExchangeAPRecognition", image, pipeline_override={
                    "ShoppingExchangeAPRecognition": {
                        "recognition": "TemplateMatch",
                        "template": file_name,
                        "roi": [27, 311, 669, 230],
                        "threshold": 0.999
                    }})

            if context.tasker.stopping:
                logger.info("任务中断")
                return True

            if reco_detail:
                box = reco_detail.best_result.box
                context.tasker.controller.post_click(box[0] + 80, box[1] + 80).wait()
                time.sleep(0.6)

                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition("ShoppingPlus", image)
                if reco_detail:
                    box = reco_detail.best_result.box
                    context.tasker.controller.post_click(box[0], box[1]).wait()

                context.run_task("ShoppingExchangeBuy")
                time.sleep(0.4)
                logger.info("购买成功")
            else:
                logger.info("未找到该物品")
        logger.info("结束购买")
        return True
