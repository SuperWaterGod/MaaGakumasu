import json
import time
from utils import logger
from collections import Counter

from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("ProduceChooseEventAuto")
class ProduceChooseEventAuto(CustomAction):
    """
        自动识别选择培育事件
        优先选择SP，没有SP则根据老师意见选择
    """

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:

        logger.success("事件: 选择事件")
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
            },
            "交谈": {
                "img": "produce/chat.png",
                "keyword": ["先生に相談して"]
            }
        }
        ocr_list = [
            keyword
            for category_info in suggestion_list.values()
            if "keyword" in category_info and isinstance(category_info["keyword"], list)
            for keyword in category_info["keyword"]
        ]
        # 识别SP课程
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition(
            "ProduceChooseEventSp", image,
            pipeline_override={"ProduceChooseEventSp": {
                "recognition": "TemplateMatch",
                "template": "produce/sp.png",
                "roi": [0, 880, 720, 220]
            }})

        if reco_detail:
            logger.info("存在SP课程")
            result = reco_detail.best_result.box
            context.tasker.controller.post_click(result[0] + 80, result[1] + 80).wait()
            time.sleep(0.5)
            context.tasker.controller.post_click(result[0] + 80, result[1] + 80).wait()
            time.sleep(3)
            return True

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
                    if "rest" in suggestion_img:
                        logger.info("选择休息")
                        context.run_task("ProduceChooseRest")
                        time.sleep(3)
                        return True
                    if "chat" in suggestion_img:
                        logger.info("选择交谈")
                        result = reco_detail.best_result.box
                        context.tasker.controller.post_click(result[0] + 80, result[1] + 80).wait()
                        time.sleep(0.5)
                        context.tasker.controller.post_click(result[0] + 80, result[1] + 80).wait()
                        context.run_task("ProduceShoppingFlag")
                        time.sleep(3)
                    result = reco_detail.best_result.box
                    context.tasker.controller.post_click(result[0] + 80, result[1] + 80).wait()
                    time.sleep(0.5)
                    context.tasker.controller.post_click(result[0] + 80, result[1] + 80).wait()
                    time.sleep(3)
                    return True

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
        if not event_existed:
            logger.info("无可用事件")
            return True

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
                    context.tasker.controller.post_click(go_out_box[0] + 80, go_out_box[1] + 80).wait()
                    time.sleep(0.5)
                    context.tasker.controller.post_click(go_out_box[0] + 80, go_out_box[1] + 80).wait()
                    time.sleep(3)
                    return True

                reco_detail = context.run_recognition("ProduceChooseRest", image)
                if reco_detail:
                    logger.info("选择休息")
                    context.run_task("ProduceChooseRest")
                    time.sleep(3)
                    return True

        reco_detail = context.run_recognition("ProduceRecognitionPoint", image)

        if reco_detail:
            point = int(reco_detail.best_result.text.replace(",", ""))
            logger.info(f"point: {point}")

            if point > 300:
                chat_box = self._get_event_box("交谈", event_existed)
                if chat_box:
                    logger.info("选择交谈")
                    context.tasker.controller.post_click(chat_box[0] + 80, chat_box[1] + 80).wait()
                    time.sleep(0.5)
                    context.tasker.controller.post_click(chat_box[0] + 80, chat_box[1] + 80).wait()
                    time.sleep(3)
                    context.run_task("ProduceShoppingFlag")
                    return True

        params = json.loads(argv.custom_action_param)

        preference = (lambda x: x if x in ["Da", "Vi", "Vo"] else "Da")((params if isinstance(params, dict) else {}).get("preference"))
        preference_box = self._get_event_box(preference, event_existed)
        lesson_box = self._get_event_box("上课", event_existed)
        event_box = self._get_event_box("活动", event_existed)
        chat_box = self._get_event_box("交谈", event_existed)
        go_out_box = self._get_event_box("外出", event_existed)

        if preference_box:
            logger.info("选择偏好属性")
            context.tasker.controller.post_click(preference_box[0] + 80, preference_box[1] + 80).wait()
            time.sleep(0.5)
            context.tasker.controller.post_click(preference_box[0] + 80, preference_box[1] + 80).wait()
            time.sleep(3)
            return True
        if lesson_box:
            logger.info("选择上课")
            context.tasker.controller.post_click(lesson_box[0] + 80, lesson_box[1] + 80).wait()
            time.sleep(0.5)
            context.tasker.controller.post_click(lesson_box[0] + 80, lesson_box[1] + 80).wait()
            time.sleep(3)
            return True
        if event_box:
            logger.info("选择活动")
            context.tasker.controller.post_click(event_box[0] + 80, event_box[1] + 80).wait()
            time.sleep(0.5)
            context.tasker.controller.post_click(event_box[0] + 80, event_box[1] + 80).wait()
            time.sleep(3)
            return True
        if chat_box:
            logger.info("选择交谈")
            context.tasker.controller.post_click(chat_box[0] + 80, chat_box[1] + 80).wait()
            time.sleep(0.5)
            context.tasker.controller.post_click(chat_box[0] + 80, chat_box[1] + 80).wait()
            time.sleep(3)
            context.run_task("ProduceShoppingFlag")
            return True
        if go_out_box:
            logger.info("选择外出")
            context.tasker.controller.post_click(go_out_box[0] + 80, go_out_box[1] + 80).wait()
            time.sleep(0.5)
            context.tasker.controller.post_click(go_out_box[0] + 80, go_out_box[1] + 80).wait()
            time.sleep(3)
            return True

        return False

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
                # print(f"卡片数量:{suggestions}/{cards}/{useless}")

                if suggestions > 0:

                    if suggestions_box[1] < 840 or suggestions_box[1] > 1150:
                        continue
                    context.tasker.controller.post_click(suggestions_box[0] + 100, suggestions_box[1] + 140).wait()
                    time.sleep(0.3)
                    context.tasker.controller.post_click(suggestions_box[0] + 100, suggestions_box[1] + 140).wait()
                    end_time = time.time()
                    logger.info("出牌 耗时:{:.2f}秒".format(end_time - start_time))
                    time.sleep(3)
                    start_time = time.time()
                elif useless > 0 and suggestions == 0 and cards == 0:
                    logger.warning("!!!!!!!!无可用牌!!!!!!!!!!!")
                    context.run_task("ProduceRecognitionSkipRound")

                end_time = time.time()
                if end_time - start_time > 10:
                    if best_box[1] < 840 or best_box[1] > 1150:
                        continue
                    logger.warning("检测超时")
                    context.tasker.controller.post_click(best_box[0], best_box[1]).wait()
                    time.sleep(0.3)
                    context.tasker.controller.post_click(best_box[0], best_box[1]).wait()
                    time.sleep(3)
                    start_time = time.time()

            else:
                reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", image)
                if not reco_detail:
                    logger.info("未检测到卡片和体力")
                    logger.success("事件: 退出出牌")
                    break

                reco_detail = context.run_recognition("ProduceYes", image)
                if reco_detail:
                    logger.info("点击确认")
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
        logger.success("事件: 商店购买")
        if reco_detail:
            for result in reco_detail.filterd_results:
                box = result.box
                context.tasker.controller.post_click(box[0], box[1] - 66).wait()
                time.sleep(0.5)
                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition(
                    "ProduceRecognitionDrinkFull", image, pipeline_override={
                        "ProduceRecognitionDrinkFull": {
                            "recognition": "TemplateMatch",
                            "template": "produce/drink_full.png",
                            "roi": [0, 1020, 720, 135]
                        }})
                if reco_detail:
                    logger.info("饮料已满，放弃购买")
                    continue
                reco_detail = context.run_recognition("ProduceShoppingBuy", image)
                if reco_detail:
                    context.run_task("ProduceShoppingBuy")
                    start_time = time.time()
                    while True:
                        context.run_task("Click_1")
                        time.sleep(0.5)
                        end_time = time.time()
                        if end_time - start_time > 3:
                            break

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
        logger.success("事件: 选择强化")
        context.tasker.controller.post_click(140, 760).wait()
        context.run_task("ProduceChooseStrengthen")
        return True
