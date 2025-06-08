import time
from utils import logger
from collections import Counter

from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("ProduceCardsAuto")
class ProduceCardsAuto(CustomAction):
    """
        自动识别根据系统提示出牌
        15秒未检测到提示牌，则打出最高分的牌
        识别不到体力退出函数
        处理是否打出该牌的弹窗
    """

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:

        start_time = time.time()
        while True:

            # 识别手牌
            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition("ProduceRecognitionCards", image)
            if reco_detail:
                results = reco_detail.all_results

                label_counts = Counter()
                suggestions_box = [0, 0]
                best_box = reco_detail.best_result.box
                for result in results:
                    label_counts[result.label] += 1
                    if result.label == "suggestions":
                        suggestions_box = result.box
                suggestions = label_counts["suggestions"]
                useless = label_counts["useless"]
                cards = label_counts["cards"]
                print(f"卡片数量:{suggestions}/{cards}/{useless}")

                if suggestions > 0:
                    context.tasker.controller.post_click(suggestions_box[0] + 100, suggestions_box[1] + 140).wait()
                    time.sleep(0.3)
                    context.tasker.controller.post_click(suggestions_box[0] + 100, suggestions_box[1] + 140).wait()
                    logger.info("------------------出牌------------------")
                    end_time = time.time()
                    logger.info("耗时:{}秒".format(end_time - start_time))
                    time.sleep(3)
                    start_time = time.time()
                elif useless > 0 and suggestions == 0 and cards == 0:
                    logger.info("!!!!!!!!无可用牌!!!!!!!!!!!")
                    context.run_task("ProduceRecognitionSkipRound")

                end_time = time.time()
                if end_time - start_time > 10:
                    logger.info("检测超时")
                    context.tasker.controller.post_click(best_box[0], best_box[1]).wait()

            else:
                reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", image)
                if not reco_detail:
                    logger.info("未检测到卡片和体力")
                    print("退出出牌")
                    break

                reco_detail = context.run_recognition("ProduceYes", image)
                if reco_detail:
                    logger.info("------------------点击确认------------------")
                    context.tasker.controller.post_click(reco_detail.best_result.box[0], reco_detail.best_result.box[1]).wait()

                reco_detail = context.run_recognition("ProduceRecognitionNoCards", image)
                if reco_detail:
                    logger.info("无手牌")
                    context.run_task("ProduceRecognitionSkipRound")

            if context.tasker.stopping:
                logger.info("任务中断")
                return True
            time.sleep(0.2)

        return True


@AgentServer.custom_action("ProduceShoppingAuto")
class ProduceShoppingAuto(CustomAction):
    """
        自动购买商店打折销售的饮料
        异常处理P点数不够
    """
    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition(
            "ProduceRecognitionSale", image, pipeline_override={
                "ProduceRecognitionSale": {
                    "recognition": "TemplateMatch",
                    "template": "produce/Sale.png",
                    "roi": [46, 479, 626, 529]
                }})
        logger.info("商店购买")
        if reco_detail:
            for result in reco_detail.filterd_results:
                box = result.box
                context.tasker.controller.post_click(box[0], box[1] - 66).wait()
                time.sleep(0.3)
                reco_detail = context.run_recognition("ProduceShoppingBuy", image)
                if reco_detail:
                    context.run_task("ProduceShoppingBuy")
                    time.sleep(5)

        return True


@AgentServer.custom_action("ProduceStrengthenAuto")
class ProduceStrengthenAuto(CustomAction):
    """
        自动选择卡牌强化
        默认选择第一张
    """
    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:
        context.tasker.controller.post_click(140, 760).wait()
        context.run_task("ProduceChooseStrengthen")
        return True
