import time
from utils import logger
from collections import Counter
from typing import Union, Optional

from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("ProduceShowStart")
class ProduceShowStart(CustomRecognition):
    """
        检测通过屏幕是否旋转判断演出开始
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        image = argv.image
        height = image.shape[0]
        width = image.shape[1]
        context.run_task("Click_1")
        if height < width:
            logger.success("事件: 演出开始")
            return CustomRecognition.AnalyzeResult(box=[0, 0, 0, 0], detail={"detail": "屏幕旋转"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕未旋转"})


@AgentServer.custom_recognition("ProduceShowEnd")
class ProduceShowEnd(CustomRecognition):
    """
        检测屏幕是否旋转
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        image = argv.image
        height = image.shape[0]
        width = image.shape[1]
        context.run_task("Click_1")
        if height > width:
            logger.success("事件: 演出结束")
            return CustomRecognition.AnalyzeResult(box=[0, 0, 0, 0], detail={"detail": "屏幕旋转"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕未旋转"})


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

        logger.success("事件: 选择卡牌")
        if reco_detail and reco_detail.hit:
            logger.info("选择建议卡")
            result = reco_detail.best_result.box
            result[1] = result[1] - 80
            return CustomRecognition.AnalyzeResult(box=result, detail={"detail": "选择建议卡"})
        else:
            logger.info("选择第一张卡")
            result = [160, 824, 20, 20]
            return CustomRecognition.AnalyzeResult(box=result, detail={"detail": "选择第一张卡"})


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
        logger.success("事件: 选择饮料")
        if reco_detail and reco_detail.hit:
            logger.info("放弃饮料")
            return CustomRecognition.AnalyzeResult(box=reco_detail.best_result.box, detail={"detail": "放弃饮料"})
        else:
            logger.info("选择第一个饮料")
            result = [160, 824, 124, 124]
            return CustomRecognition.AnalyzeResult(box=result, detail={"detail": "选择第一个饮料"})


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
        logger.success("事件: 选择物品")
        logger.info("选择第一个物品")
        result = [160, 824, 124, 124]
        return CustomRecognition.AnalyzeResult(box=result, detail={"detail": "选择第一个物品"})


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

        context.run_task("Click_1")
        cards_reco_detail = context.run_recognition("ProduceRecognitionCards", argv.image)
        health_reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", argv.image)
        if cards_reco_detail and cards_reco_detail.hit and health_reco_detail and health_reco_detail.hit:
            logger.success("事件: 出牌场景")
            return CustomRecognition.AnalyzeResult(box=[0, 0, 0, 0], detail={"detail": "识别到出牌场景"})
        else:
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到选择场景"})


@AgentServer.custom_recognition("ProduceOptionsFlagAuto")
class ProduceOptionsFlagAuto(CustomRecognition):
    """
        自动识别选择冲刺/上课/外出场景
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        context.run_task("Click_1")
        options_reco_detail = context.run_recognition("ProduceRecognitionOptions", argv.image)
        # 通过识别右上角的“审查基准”去除大量场景，但注意N.I.A没有这4个字，适配N.I.A时注意修改判断或增加识别模板
        parameter_reco_detail = context.run_recognition("ProduceRecognitionParameterFlag", argv.image)
        if (not options_reco_detail or not options_reco_detail.hit or
                not parameter_reco_detail or not parameter_reco_detail.hit or
                options_reco_detail.best_result.box[2] < 490):
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到选择场景"})

        logger.success("事件: 选择冲刺/上课/外出")
        results = options_reco_detail.all_results
        label_counts = Counter()
        best_box = options_reco_detail.best_result.box
        click_point_x = best_box[0] + best_box[2] // 2
        click_point_y = best_box[1] + best_box[3] // 2
        for result in results:
            label_counts[result.label] += 1
        choose = label_counts["choose"]
        lesson = label_counts["lesson"]
        logger.info(f"选项数量:{choose}/{lesson}")
        if lesson == 0:
            pass
        context.tasker.controller.post_click(click_point_x, click_point_y).wait()
        time.sleep(0.2)
        context.tasker.controller.post_click(click_point_x, click_point_y).wait()
        return CustomRecognition.AnalyzeResult(box=best_box, detail={"detail": "选择加最佳选项"})
