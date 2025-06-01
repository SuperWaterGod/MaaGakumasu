import json
import time
from math import ceil
from utils import logger
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
                if reco_detail:
                    coins.append(
                        int(reco_detail.best_result.text.replace(",", "")))
                else:
                    coins.append(None)

        count = 0
        logger.info(coins)
        for i in range(len(coins) - 1, -1, -1):
            if coins[i] is None:
                count += 1
            else:
                break
        max_page = ceil(len(coins) / 2) - int(count / 2)
        for i in range(max_page):
            logger.info(f"第{i + 1}页")
            roi = None
            if i == max_page - 1 and coins[-1] is None and (len(coins) - count) % 2 == 1:
                if coins[0 + i * 2] is not None and coins[0 + i * 2] >= 10:
                    # print(f"商品{1 + i * 2} 购买下面的")
                    roi = [3, 730, 720, 550]
                else:
                    # print(f"商品{1 + i * 2} 不购买")
                    pass
            else:
                if coins[0 + i * 2] is not None and coins[0 + i * 2] >= 10:
                    # print(f"商品{1 + i * 2} 购买上面的")
                    roi = [0, 230, 720, 500]
                else:
                    # print(f"商品{1 + i * 2} 不购买")
                    pass

            if roi:
                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition("ShoppingCoinButton", image,
                                                      pipeline_override={"ShoppingCoinButton": {"roi": roi}})
                if reco_detail:
                    box = reco_detail.best_result.box
                    context.tasker.controller.post_click(box[0], box[1]).wait()
                    time.sleep(0.5)
                    logger.info("开始购买")
                    context.run_task("ShoppingCoinBuy")

            roi = None
            if coins[1 + i * 2] is not None and coins[1 + i * 2] >= 10:
                # print(f"商品{2 + i * 2} 购买下面的")
                roi = [3, 730, 720, 550]
            else:
                # print(f"商品{2 + i * 2} 不购买")
                pass

            if roi:
                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition("ShoppingCoinButton", image,
                                                      pipeline_override={"ShoppingCoinButton": {"roi": roi}})
                if reco_detail:
                    box = reco_detail.best_result.box
                    context.tasker.controller.post_click(box[0], box[1]).wait()
                    time.sleep(0.6)
                    logger.info("开始购买")
                    context.run_task("ShoppingCoinBuy")

            if i + 1 == max_page:
                break
            logger.info("翻页")
            context.run_task("ShoppingCoinNext")
            time.sleep(0.5)
        return True


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
                        context.run_task("ShoppingPlus")
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
                        "threshold": 0.95
                    }})
            if reco_detail:
                box = reco_detail.best_result.box
                context.tasker.controller.post_click(box[0] + 80, box[1] + 80).wait()
                time.sleep(0.5)
                context.run_task("ShoppingPlus")
                context.run_task("ShoppingExchangeBuy")
                time.sleep(0.5)
                logger.info("购买成功")
            else:
                logger.info("未找到该物品")
        logger.info("结束购买")
        return True
