import json
import time
from utils import logger
from collections import Counter
from typing import Union, Optional

from maa.define import RectType
from maa.context import Context
from maa.agent.agent_server import AgentServer
from maa.custom_recognition import CustomRecognition


@AgentServer.custom_recognition("ProduceChooseEventAuto")
class ProduceChooseEventAuto(CustomRecognition):
    """
        自动识别选择培育事件
        优先选择SP，没有SP则根据老师意见选择
    """

    def analyze(
            self,
            context: Context,
            argv: CustomRecognition.AnalyzeArg,
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        reco_detail = context.run_recognition("ProduceChooseEventFlag", argv.image)
        if not reco_detail:
            return None
        logger.info("进入事件选项")
        suggestion_list = {
            "Vo": {
                "img": "produce/Vo.png",
                "keyword": ["ボーカル", "唱歌"]
            },
            "Da": {
                "img": "produce/Da.png",
                "keyword": ["ダンス", "舞蹈"]
            },
            "Vi": {
                "img": "produce/Vi.png",
                "keyword": ["ビジュアル", "视觉"]
            },
            "体力": {
                "img": "produce/rest.png",
                "keyword": ["体力"]
            }
        }
        ocr_list = [
            keyword
            for category_info in suggestion_list.values()
            if "keyword" in category_info and isinstance(category_info["keyword"], list)
            for keyword in category_info["keyword"]
        ]
        # 识别SP课程
        reco_detail = context.run_recognition(
            "ProduceChooseEventSp", argv.image,
            pipeline_override={"ProduceChooseEventSp": {
                "recognition": "TemplateMatch",
                "template": "produce/sp.png",
                "roi": [0, 880, 720, 220]
            }})

        if reco_detail:
            logger.info("存在SP课程")
            result = reco_detail.best_result.box
            context.tasker.controller.post_click(result[0] + 80, result[1] + 80).wait()
            return CustomRecognition.AnalyzeResult(box=result, detail="存在SP课程")

        time.sleep(0.2)
        # 识别老师建议
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition(
            "ProduceChooseEventSuggestion", image,
            pipeline_override={"ProduceChooseEventSuggestion": {
                "recognition": "OCR",
                "expected": ocr_list,
                "roi": [270, 160, 350, 56]
            }})
        # "roi" : [230,171,391,70]
        if reco_detail:
            suggestion_str = "".join(item.text for item in reco_detail.filterd_results)
            logger.info(suggestion_str)
            suggestion_img = self._find_image_from_phrase(suggestion_str, suggestion_list)
            logger.info(suggestion_img)

            if suggestion_img:
                # 识别建议课程
                reco_detail = context.run_recognition(
                    "ProduceChooseSuggestion", image,
                    pipeline_override={"ProduceChooseSuggestion": {
                        "recognition": "TemplateMatch",
                        "template": suggestion_img,
                        "roi": [0, 800, 720, 256]
                    }})
                if reco_detail:
                    logger.info("存在建议课程")
                    result = reco_detail.best_result.box
                    context.tasker.controller.post_click(result[0] + 30, result[1] + 30).wait()
                    return CustomRecognition.AnalyzeResult(box=result, detail="存在建议课程")

        event_list = {
            "Da": "produce/Da.png",
            "Vi": "produce/Vi.png",
            "Vo": "produce/Vo.png",
            "交谈": "produce/chat.png",
            "上课": "produce/lesson.png",
            "活动": "produce/event.png",
            "外出": "produce/go_out.png"
        }

        event_existed = []

        for event_name, event_img in event_list.items():
            reco_detail = context.run_recognition(
                "ProduceRecognitionEvent", image,
                pipeline_override={"ProduceRecognitionEvent": {
                    "recognition": "TemplateMatch",
                    "template": event_img,
                    "roi": [0, 880, 720, 220]
                }})
            if reco_detail:
                event_existed.append({
                    event_name: reco_detail.best_result.box,
                })
        logger.info(event_existed)

        reco_detail = context.run_recognition("ProduceRecognitionHealth", image)
        if reco_detail:
            health_detail = reco_detail.best_result.text.split("/")
            health = int(health_detail[0])
            health_total = int(health_detail[1])
            logger.info(f"{health}/{health_total}")

            if health / health_total < 0.3:
                logger.info("体力过低")
                go_out_box = self._get_event_box("外出", event_existed)
                if go_out_box:
                    logger.info("选择外出")
                    return CustomRecognition.AnalyzeResult(box=go_out_box, detail="选择外出")

                reco_detail = context.run_recognition(
                    "ProduceChooseRest", argv.image,
                    pipeline_override={"ProduceChooseRest": {
                        "recognition": "TemplateMatch",
                        "template": "produce/rest.png"
                    }})
                if reco_detail:
                    logger.info("选择休息")
                    result = reco_detail.best_result.box
                    return CustomRecognition.AnalyzeResult(box=result, detail="选择休息")

        reco_detail = context.run_recognition("ProduceRecognitionPoint", image)

        if reco_detail:
            point = int(reco_detail.best_result.text.replace(",", ""))
            logger.info(f"point: {point}")

            if point > 300:
                chat_box = self._get_event_box("交谈", event_existed)
                if chat_box:
                    logger.info("选择交谈")
                    return CustomRecognition.AnalyzeResult(box=chat_box, detail="选择交谈")

        params = json.loads(argv.custom_recognition_param)

        preference = (lambda x: x if x in ["Da", "Vi", "Vo"] else "Da")((params if isinstance(params, dict) else {}).get("preference"))
        preference_box = self._get_event_box(preference, event_existed)
        lesson_box = self._get_event_box("上课", event_existed)
        event_box = self._get_event_box("活动", event_existed)
        chat_box = self._get_event_box("交谈", event_existed)
        go_out_box = self._get_event_box("外出", event_existed)

        if preference_box:
            logger.info("选择偏好属性")
            context.tasker.controller.post_click(preference_box[0] + 80, preference_box[1] + 80).wait()
            return CustomRecognition.AnalyzeResult(box=preference_box, detail="选择偏好属性")
        if lesson_box:
            logger.info("选择上课")
            context.tasker.controller.post_click(lesson_box[0] + 80, lesson_box[1] + 80).wait()
            return CustomRecognition.AnalyzeResult(box=lesson_box, detail="选择上课")
        if event_box:
            logger.info("选择活动")
            context.tasker.controller.post_click(event_box[0] + 80, event_box[1] + 80).wait()
            return CustomRecognition.AnalyzeResult(box=event_box, detail="选择活动")
        if chat_box:
            logger.info("选择交谈")
            context.tasker.controller.post_click(chat_box[0] + 80, chat_box[1] + 80).wait()
            return CustomRecognition.AnalyzeResult(box=chat_box, detail="选择交谈")
        if go_out_box:
            logger.info("选择外出")
            context.tasker.controller.post_click(go_out_box[0] + 80, go_out_box[1] + 80).wait()
            return CustomRecognition.AnalyzeResult(box=go_out_box, detail="选择外出")

        return CustomRecognition.AnalyzeResult(box=None, detail="未能识别到选项")

    @staticmethod
    def _find_image_from_phrase(target_phrase, data_dict):
        """
        检测给定的短语（句子）包含字典中哪个类别的关键词，并返回对应的图片路径。

        Args:
            target_phrase (str): 要查找的短语或句子。
            data_dict (dict): 包含分类信息的字典，其值是包含 'img' 和 'keyword' 的字典。

        Returns:
            str or None: 如果短语包含匹配的类别关键词，则返回对应的 'img' 值；否则返回 None。
                          遵循“以第一个为准”的原则。
        """
        for category_key, category_info in data_dict.items():
            if "keyword" in category_info and isinstance(category_info["keyword"], list):
                category_keywords = category_info["keyword"]
                category_img = category_info.get("img")

                for keyword in category_keywords:
                    if keyword in target_phrase:
                        return category_img
        return None

    @staticmethod
    def _get_event_box(data: str, data_list: list):
        """
        遍历列表中的每个字典，如果找到任何一个字典包含 'data' 键，

        Args:
            data_list (list): 包含字典的列表，例如 [{'上课': [...]}, {'xxx': [...]}]。

        Returns:
            list: 如果找到 'data' 键，返回其对应的值（一个列表）。
            None: 如果列表为空，或者列表中没有字典，或者所有字典中都没有 'data' 键。
        """
        if not data_list:
            return None
        for item in data_list:
            data_value = item.get(data)
            if data_value is not None:
                return data_value
        return None


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
