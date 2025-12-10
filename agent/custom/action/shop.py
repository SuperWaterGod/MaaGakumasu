import time
from utils import logger

from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("ShoppingCoinGachaAuto")
class ShoppingCoinGachaAuto(CustomAction):
    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:
        params = {
            "friend": {
                "name": "好友",
                "enabled": context.get_node_data("ShoppingCoinGachaFriendGacha").get("enabled", True),
                "roi": [344, 26, 150, 44],
                "count": 0,
                "page": 1
            },
            "sense": {
                "name": "感性",
                "enabled": context.get_node_data("ShoppingCoinGachaSenseGacha").get("enabled", False),
                "roi": [546, 26, 150, 44],
                "count": 0,
                "page": 1
            },
            "logic": {
                "name": "理性",
                "enabled": context.get_node_data("ShoppingCoinGachaLogicGacha").get("enabled", False),
                "roi": [344, 84, 660, 150],
                "count": 0,
                "page": 2
            },
            "anomaly": {
                "name": "非凡",
                "enabled": context.get_node_data("ShoppingCoinGachaAnomalyGacha").get("enabled", False),
                "roi": [546, 84, 660, 150],
                "count": 0,
                "page": 2
            }
        }
        page = 1
        image = context.tasker.controller.post_screencap().wait().get()
        for key in params.keys():
            if context.tasker.stopping:
                logger.warning("任务中断")
                return True

            if params[key]["enabled"]:
                reco_detail = context.run_recognition(
                    "ShoppingCoinGachaCount", image,
                    pipeline_override={"ShoppingCoinGachaCount": {
                        "recognition": "OCR",
                        "expected": "^\\d{1,3}(,\\d{3})*$",
                        "roi": params[key]["roi"]
                    }})
                if reco_detail.best_result:
                    params[key]["count"] = int(reco_detail.best_result.text.replace(",", ""))
                else:
                    params[key]["count"] = 0
                logger.info(f"{params[key]['name']}扭蛋数量:{params[key]['count']}")
                if params[key]["count"] < 10:
                    logger.info(f"{params[key]['name']}扭蛋数量不足10，跳过购买")
                    continue
                if params[key]["page"] > page:
                    page = params[key]["page"]
                    logger.info(f"切换到第{page}页")
                    context.run_task("ShoppingNextPage")
                logger.info("开始购买")
                context.run_task("ShoppingCoinGachaBuy", pipeline_override={
                    "ShoppingCoinGachaBuy": {
                        "template": f"shopping_gacha_{key}.png"
                    }
                })

        logger.info("结束扭蛋购买")
        return True


@AgentServer.custom_action("ShoppingDailyExchangeMoneyAuto")
class ShoppingDailyExchangeMoneyAuto(CustomAction):

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:

        params = {
            "recommend": context.get_node_data("ShoppingDailyExchangeItemsRecommend").get("enabled", True),
            "sense_blue": context.get_node_data("ShoppingDailyExchangeItemsSenseBlue").get("enabled", False),
            "sense_red": context.get_node_data("ShoppingDailyExchangeItemsSenseRed").get("enabled", False),
            "sense_yellow": context.get_node_data("ShoppingDailyExchangeItemsSenseYellow").get("enabled", False),
            "logic_blue": context.get_node_data("ShoppingDailyExchangeItemsLogicBlue").get("enabled", False),
            "logic_red": context.get_node_data("ShoppingDailyExchangeItemsLogicRed").get("enabled", False),
            "logic_yellow": context.get_node_data("ShoppingDailyExchangeItemsLogicYellow").get("enabled", False),
            "anomaly_blue": context.get_node_data("ShoppingDailyExchangeItemsAnomalyBlue").get("enabled", False),
            "anomaly_red": context.get_node_data("ShoppingDailyExchangeItemsAnomalyRed").get("enabled", False),
            "anomaly_yellow": context.get_node_data("ShoppingDailyExchangeItemsAnomalyYellow").get("enabled", False),
            "lesson_note": context.get_node_data("ShoppingDailyExchangeItemsLessonNote").get("enabled", False),
            "veteran_note": context.get_node_data("ShoppingDailyExchangeItemsVeteranNote").get("enabled", False),
            "support_point": context.get_node_data("ShoppingDailyExchangeItemsSupportPoint").get("enabled", True),
            "challenge_ticket": context.get_node_data("ShoppingDailyExchangeItemsChallengeTicket").get("enabled", False),
            "record_key": context.get_node_data("ShoppingDailyExchangeItemsRecordKey").get("enabled", False),
            "hanami_ume_shards": context.get_node_data("ShoppingDailyExchangeItemsHanamiUme").get("enabled", False),
            "katuragi_ririya_shards": context.get_node_data("ShoppingDailyExchangeItemsKaturagiRiriya").get("enabled", False),
            "sasazawa_hiro_shards": context.get_node_data("ShoppingDailyExchangeItemsSasazawaHiro").get("enabled", False),
            "sion_sumika_shards": context.get_node_data("ShoppingDailyExchangeItemsSionSumika").get("enabled", False),
            "tukimura_temari_shards": context.get_node_data("ShoppingDailyExchangeItemsTukimuraTemari").get("enabled", False),
            "huzita_kotone_shards": context.get_node_data("ShoppingDailyExchangeItemsHuzitaKotone").get("enabled", False),
            "kuramoto_tina_shards": context.get_node_data("ShoppingDailyExchangeItemsHanamiSaki").get("enabled", False),
            "hanami_saki_shards": context.get_node_data("ShoppingDailyExchangeItemsKuramotoTina").get("enabled", False),
            "arimura_mao_shards": context.get_node_data("ShoppingDailyExchangeItemsArimuraMao").get("enabled", False),
            "himezaki_rinami_shards": context.get_node_data("ShoppingDailyExchangeItemsHimezakiRinami").get("enabled", False)
        }

        wishlist = []
        for key, value in params.items():
            if value:
                wishlist.append((key, value))
        if not wishlist:
            logger.info("没有选择任何金币物品，跳过购买")
            return True
        logger.info("购买金币物品")
        max_page = 2
        for i in range(max_page):
            logger.info(f"第{i + 1}页")
            time.sleep(2)
            image = context.tasker.controller.post_screencap().wait().get()
            for key, value in wishlist:
                if key == "recommend":
                    logger.info("购买推荐物品")
                    file_name = f"shopping_recommend.png"
                else:
                    logger.info(f"购买{key}")
                    file_name = f"items/{key}.png"

                reco_detail = context.run_recognition(
                    "ShoppingDailyExchangeMoneyRecognition", image, pipeline_override={
                        "ShoppingDailyExchangeMoneyRecognition": {
                            "recognition": "TemplateMatch",
                            "template": file_name,
                            "roi": [30, 288, 660, 668],
                            "threshold": 0.93
                        }})

                if context.tasker.stopping:
                    logger.warning("任务中断")
                    return True

                if reco_detail.best_result:
                    for result in reco_detail.filtered_results:
                        box = result.box
                        context.tasker.controller.post_click(box[0] + 70, box[1] + 70).wait()
                        time.sleep(0.5)

                        image_plus = context.tasker.controller.post_screencap().wait().get()
                        reco_detail = context.run_recognition("ShoppingPlus", image_plus)
                        if reco_detail.best_result:
                            box = reco_detail.best_result.box
                            context.tasker.controller.post_click(box[0], box[1]).wait()
                        context.run_task("ShoppingDailyExchangeBuy")
                        time.sleep(0.8)

                    time.sleep(0.5)
                else:
                    # 未找到该物品
                    pass
            if i + 1 == max_page:
                break
            context.run_task("ShoppingNextPage")

        logger.info("结束购买")
        return True


@AgentServer.custom_action("ShoppingDailyExchangeAPAuto")
class ShoppingDailyExchangeAPAuto(CustomAction):

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:

        params = {
            "support_point_increased": context.get_node_data("ShoppingDailyExchangeAPSupportPointIncreased").get("enabled", False),
            "note_increased": context.get_node_data("ShoppingDailyExchangeAPNoteIncreased").get("enabled", False),
            "challenge_ticket": context.get_node_data("ShoppingDailyExchangeAPChallengeTicket").get("enabled", False),
            "memory_ticket": context.get_node_data("ShoppingDailyExchangeAPMemoryTicket").get("enabled", False)
        }

        wishlist = []
        for key, value in params.items():
            if value:
                wishlist.append((key, value))
        if not wishlist:
            logger.info("没有选择任何AP物品，跳过购买")
            return True
        logger.info("购买AP物品")
        items_image = context.tasker.controller.post_screencap().wait().get()
        for key, value in wishlist:
            logger.info(f"购买{key}")
            file_name = f"items/{key}.png"
            reco_detail = context.run_recognition(
                "ShoppingDailyExchangeAPRecognition", items_image, pipeline_override={
                    "ShoppingDailyExchangeAPRecognition": {
                        "recognition": "TemplateMatch",
                        "template": file_name,
                        "roi": [27, 311, 669, 230],
                        "threshold": 0.98
                    }})

            if context.tasker.stopping:
                logger.warning("任务中断")
                return True

            if reco_detail.best_result:
                box = reco_detail.best_result.box
                context.tasker.controller.post_click(box[0] + 80, box[1] + 80).wait()
                time.sleep(0.8)
                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition("ShoppingPlus", image)
                if reco_detail.best_result:
                    box = reco_detail.best_result.box
                    context.tasker.controller.post_click(box[0], box[1]).wait()
                context.run_task("ShoppingDailyExchangeBuy")
                time.sleep(0.5)
                # 购买成功
            else:
                # 未找到该物品
                pass

        logger.info("结束购买")
        return True
