import json
import time
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
        for i in range(max_page):
            logger.info(f"第{i + 1}页")
            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition("ShoppingCoinButton", image)
            for result in reco_detail.filterd_results:
                box = result.box
                context.tasker.controller.post_click(box[0], box[1]).wait()
                time.sleep(0.2)
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

                if reco_detail:
                    box = reco_detail.best_result.box
                    context.tasker.controller.post_click(box[0] + 70, box[1] + 70).wait()
                    time.sleep(0.5)
                    context.run_task("ShoppingExchangeBuy")
                    time.sleep(0.5)
                    logger.info("购买成功")
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
                context.run_task("ShoppingExchangeBuy")
                time.sleep(0.5)
                logger.info("购买成功")
            else:
                logger.info("未找到该物品")
        logger.info("结束购买")
        return True
