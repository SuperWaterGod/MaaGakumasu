import time
import json
from utils import logger
from collections import Counter
from typing import Union, Optional
from difflib import SequenceMatcher

from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("ProduceChooseIdolAuto")
class ProduceChooseIdolAuto(CustomRecognition):
    """
        自动识别当前偶像名称和歌曲
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        idol_name = json.loads(argv.custom_recognition_param)["idol_name"]
        song_name = json.loads(argv.custom_recognition_param)["song_name"]
        recognized_name = ""
        recognized_song = ""

        true_end_detail = context.run_recognition(
            "ProduceChooseIdolTrueEnd", argv.image,
            pipeline_override={"ProduceChooseIdolTrueEnd": {
                "recognition": "OCR",
                "expected": ["True", "End"],
                "roi": [430, 34, 266, 48]
            }})
        if true_end_detail and true_end_detail.hit:
            logger.debug("识别到True End")
            idol_name_roi = [440, 128, 280, 64]
            song_name_roi = [380, 90, 320, 45]
        else:
            logger.debug("未识别到True End")
            idol_name_roi = [400, 98, 320, 64]
            song_name_roi = [340, 60, 380, 45]

        name_detail = context.run_recognition(
            "ProduceChooseIdolName", argv.image,
            pipeline_override={"ProduceChooseIdolName": {
                "recognition": "OCR",
                "roi": idol_name_roi
            }})
        if name_detail and name_detail.hit:
            recognized_name = "".join([item.text for item in name_detail.all_results]).replace(" ", "")
            logger.debug(f"识别到偶像名称: {recognized_name}")

        song_detail = context.run_recognition(
            "ProduceChooseIdolSong", argv.image,
            pipeline_override={"ProduceChooseIdolSong": {
                "recognition": "OCR",
                "roi": song_name_roi
            }})
        if song_detail and song_detail.hit:
            recognized_song = "".join([item.text for item in song_detail.all_results]).replace("[", "").replace("]", "")
            logger.debug(f"识别到歌曲名称: {recognized_song}")

        if (self.similarity_ratio(recognized_name, idol_name) >= 0.9 and
                self.similarity_ratio(recognized_song, song_name) >= 0.7):
            return CustomRecognition.AnalyzeResult(box=[0, 0, 0, 0], detail={"detail": "识别偶像卡成功"})
        else:
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "识别偶像卡失败"})

    @staticmethod
    def similarity_ratio(str1, str2):
        """返回0-1之间的相似度分数，1表示完全相同"""
        return SequenceMatcher(None, str1, str2).ratio()


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
        根据配置的优先级选择卡牌
        配置示例:
        {
            "attach": {
                "priority": ["suggestion", "event"]
            }
        }
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        logger.success("事件: 选择卡牌")

        # 从节点中获取配置的优先级
        node_data = context.get_node_data("ProduceChooseCards")
        priority = node_data.get("attach", {}).get("priority", [])

        # 根据优先级遍历
        for card_type in priority:
            if card_type == "suggestion":
                box = self._get_suggestion_card(context, argv)
                if box:
                    logger.info("选择建议卡")
                    return CustomRecognition.AnalyzeResult(box=box, detail={"detail": "选择建议卡"})
            elif card_type == "event":
                box = self._get_event_card(context, argv)
                if box:
                    logger.info("选择活动卡")
                    return CustomRecognition.AnalyzeResult(box=box, detail={"detail": "选择活动卡"})

        # 遍历没有结果，只好选择第一张卡
        logger.info("选择第一张卡")
        result = [160, 824, 20, 20]
        return CustomRecognition.AnalyzeResult(box=result, detail={"detail": "选择第一张卡"})

    @staticmethod
    def _get_event_card(context: Context, argv: CustomRecognition.AnalyzeArg):
        reco_detail_event = context.run_recognition(
            "ProduceChooseEventCards", argv.image,
            pipeline_override={"ProduceChooseEventCards": {
                "recognition": "TemplateMatch",
                "template": "produce/event_recommend.png",
                "roi": [86, 788, 545, 220]
            }})
        if reco_detail_event and reco_detail_event.hit:
            result = reco_detail_event.best_result.box
            result[1] = result[1] + 80
            return result
        return None

    @staticmethod
    def _get_suggestion_card(context: Context, argv: CustomRecognition.AnalyzeArg):
        reco_detail_recommend = context.run_recognition(
            "ProduceChooseCardsSuggestion", argv.image,
            pipeline_override={"ProduceChooseCardsSuggestion": {
                "recognition": "TemplateMatch",
                "template": "produce/recommed.png",
                "roi": [86, 788, 545, 220]
            }})
        if reco_detail_recommend and reco_detail_recommend.hit:
            result = reco_detail_recommend.best_result.box
            result[1] = result[1] - 80
            return result
        return None

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
        if (not options_reco_detail or not options_reco_detail.hit or
            options_reco_detail.best_result.box[1] < 600 or options_reco_detail.best_result.box[1] > 900 or
                options_reco_detail.best_result.box[0] > 360 or options_reco_detail.best_result.score < 0.8):
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
        time.sleep(0.5)
        context.tasker.controller.post_click(click_point_x, click_point_y).wait()
        time.sleep(0.2)
        context.tasker.controller.post_click(click_point_x, click_point_y).wait()
        return CustomRecognition.AnalyzeResult(box=best_box, detail={"detail": "选择加最佳选项"})
