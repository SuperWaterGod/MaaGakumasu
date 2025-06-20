from utils import logger
from collections import Counter
from typing import Union, Optional

from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("ProduceChooseCardsAuto")
class ProduceChooseCardsAuto(CustomRecognition):
    """
        自动识别选择卡牌
        优先选择推荐，没有推荐则根据培育对象选择
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        reco_detail = context.run_recognition(
            "ProduceChooseCardsSuggestion", argv.image,
            pipeline_override={"ProduceChooseCardsSuggestion": {
                "recognition": "TemplateMatch",
                "template": "produce/recommed.png",
                "roi": [86, 788, 545, 220]
            }})

        if reco_detail:
            logger.info("选择建议卡")
            result = reco_detail.best_result.box
            result[1] = result[1] - 80
            return CustomRecognition.AnalyzeResult(box=result, detail="选择建议卡")
        else:
            logger.info("选择第一张卡")
            result = [160, 824, 124, 124]
            return CustomRecognition.AnalyzeResult(box=result, detail="选择第一张卡")


@AgentServer.custom_recognition("ProduceChooseDrinkAuto")
class ProduceChooseDrinkAuto(CustomRecognition):
    """
        自动识别选择饮料
        优先选择第一个
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        reco_detail = context.run_recognition(
            "ProduceChooseDrinkFull", argv.image,
            pipeline_override={"ProduceChooseDrinkFull": {
                "recognition": "TemplateMatch",
                "template": "produce/drink_reject.png",
                "roi": [54, 950, 610, 98]
            }})

        if reco_detail:
            logger.info("放弃饮料")
            return CustomRecognition.AnalyzeResult(box=reco_detail.best_result.box, detail="放弃饮料")
        else:
            logger.info("选择第一个饮料")
            result = [160, 824, 124, 124]
            return CustomRecognition.AnalyzeResult(box=result, detail="选择第一个饮料")


@AgentServer.custom_recognition("ProduceChooseItemAuto")
class ProduceChooseItemAuto(CustomRecognition):
    """
        自动识别选择饮料
        优先选择第一个
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        logger.info("选择第一个物品")
        result = [160, 824, 124, 124]
        return CustomRecognition.AnalyzeResult(box=result, detail="选择第一个物品")


@AgentServer.custom_recognition("ProduceCardsFlagAuto")
class ProduceCardsFlagAuto(CustomRecognition):
    """
        自动识别出牌场景
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        cards_reco_detail = context.run_recognition("ProduceRecognitionCards", argv.image)
        health_reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", argv.image)
        if cards_reco_detail and health_reco_detail:
            logger.info("识别到出牌场景")
            return CustomRecognition.AnalyzeResult(box=[0, 0, 0, 0], detail="识别到出牌场景")
        else:
            return CustomRecognition.AnalyzeResult(box=None, detail="未识别到选择场景")


@AgentServer.custom_recognition("ProduceOptionsFlagAuto")
class ProduceOptionsFlagAuto(CustomRecognition):
    """
        自动识别选择场景
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        options_reco_detail = context.run_recognition("ProduceRecognitionOptions", argv.image)
        health_reco_detail = context.run_recognition("ProduceRecognitionOptionsEvents", argv.image)
        if not options_reco_detail or not health_reco_detail:
            return CustomRecognition.AnalyzeResult(box=None, detail="未识别到选择场景")

        logger.info("识别到选择场景")
        results = options_reco_detail.all_results
        label_counts = Counter()
        best_box = options_reco_detail.best_result.box
        for result in results:
            label_counts[result.label] += 1
        choose = label_counts["choose"]
        lesson = label_counts["lesson"]
        print(f"选项数量:{choose}/{lesson}")
        if lesson == 0:
            print("")
        context.tasker.controller.post_click(best_box[0], best_box[1]).wait()
        return CustomRecognition.AnalyzeResult(box=best_box, detail="选择加最佳选项")
