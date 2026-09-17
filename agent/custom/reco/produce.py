import os
import json
import time
import unicodedata
from typing import Tuple, Union, Optional
from difflib import SequenceMatcher

from utils import logger
from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("ProduceChooseIdolAuto")
class ProduceChooseIdolAuto(CustomRecognition):
    """
    自动识别当前偶像名称和歌曲
    """

    # 文本相似度阈值，低于该值视为识别失败
    SIMILARITY_THRESHOLD = 0.7

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
            "ProduceChooseIdolTrueEnd",
            argv.image,
            pipeline_override={
                "ProduceChooseIdolTrueEnd": {
                    "recognition": "OCR",
                    "expected": ["True", "End"],
                    "roi": [430, 34, 266, 48],
                }
            },
        )
        if true_end_detail and true_end_detail.hit:
            logger.debug("识别到True End")
            idol_name_roi = [440, 128, 280, 64]
            song_name_roi = [380, 90, 320, 45]
        else:
            logger.debug("未识别到True End")
            idol_name_roi = [400, 98, 320, 64]
            song_name_roi = [340, 60, 380, 45]

        name_detail = context.run_recognition(
            "ProduceChooseIdolName",
            argv.image,
            pipeline_override={"ProduceChooseIdolName": {"recognition": "OCR", "roi": idol_name_roi}},
        )
        name_score = 0.0
        if name_detail and name_detail.hit:
            recognized_name = "".join([item.text for item in name_detail.all_results]).replace(" ", "")
            name_score = self.similarity_ratio(recognized_name, idol_name)
            logger.info(f"识别到偶像名称: {recognized_name}，相似度: {name_score:.2f}")

        song_detail = context.run_recognition(
            "ProduceChooseIdolSong",
            argv.image,
            pipeline_override={"ProduceChooseIdolSong": {"recognition": "OCR", "roi": song_name_roi}},
        )
        song_score = 0.0
        if song_detail and song_detail.hit:
            recognized_song = "".join([item.text for item in song_detail.all_results]).replace("[", "").replace("]", "")
            song_score = self.similarity_ratio(recognized_song, song_name)
            logger.info(f"识别到歌曲名称: {recognized_song}，相似度: {song_score:.2f}")

        if name_score >= self.SIMILARITY_THRESHOLD and song_score >= self.SIMILARITY_THRESHOLD:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "识别偶像卡成功"})
        else:
            logger.debug(f"识别偶像卡失败，偶像相似度: {name_score:.2f}，歌曲相似度: {song_score:.2f}")
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "识别偶像卡失败"})

    @staticmethod
    def normalize_text(text: str) -> str:
        """
        归一化 OCR 文本，抹平特殊符号带来的差异

        OCR 对特殊符号识别不稳定，例如「36℃ U･B･U」常被识别成「36°C U・B・U」，
        直接比较相似度只有 0.63，会导致该偶像卡无法被识别。
        NFKC 会先把全角转半角、把 ℃ 拆成 °C、把 ･ 转成 ・，
        之后只保留字母、数字、假名与汉字，符号与空格一律忽略。
        """
        return "".join(ch for ch in unicodedata.normalize("NFKC", text) if ch.isalnum())

    @staticmethod
    def similarity_ratio(str1, str2):
        """返回0-1之间的相似度分数，1表示完全相同（比较前先归一化，忽略符号差异）"""
        return SequenceMatcher(
            None,
            ProduceChooseIdolAuto.normalize_text(str1),
            ProduceChooseIdolAuto.normalize_text(str2),
        ).ratio()


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
        context.run_action("Click_1")
        height, width = image.shape[0], image.shape[1]
        if height < width:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "屏幕旋转"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕未旋转"})


@AgentServer.custom_recognition("ProduceShowEnd")
class ProduceShowEnd(CustomRecognition):
    """
    检测通过屏幕是否旋转判断演出结束
    """

    def analyze(
        self,
        context: Context,
        argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        image = argv.image
        height, width = image.shape[0], image.shape[1]
        context.run_action("Click_1")
        if height > width:
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "屏幕旋转"})
        return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "屏幕未旋转"})


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
        context.run_action("Click_1")
        cards_reco_detail = context.run_recognition("ProduceRecognitionCards", argv.image)
        health_reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", argv.image)
        if cards_reco_detail and cards_reco_detail.hit and health_reco_detail and health_reco_detail.hit:
            logger.success("事件: 出牌场景")
            return CustomRecognition.AnalyzeResult(box=[0, 0, 1, 1], detail={"detail": "识别到出牌场景"})
        else:
            return CustomRecognition.AnalyzeResult(box=None, detail={"detail": "未识别到选择场景"})
