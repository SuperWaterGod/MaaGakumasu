import json
import time
from utils import logger
from collections import Counter
from typing import Dict, List, Optional, Any

from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("ProduceChooseEventAuto")
class ProduceChooseEventAuto(CustomAction):
    """
    自动识别选择培育事件
    优先选择SP，没有SP则根据老师意见选择
    然后检测体力和点数
    最后根据偏好设置选择事件
    """

    # 常量定义
    SUGGESTION_CONFIG = {
        "Vo": {"img": "produce/Vo.png", "keyword": ["ボーカル", "唱歌"]},
        "Da": {"img": "produce/Da.png", "keyword": ["ダンス", "舞蹈"]},
        "Vi": {"img": "produce/Vi.png", "keyword": ["ビジュアル", "视觉"]},
        "体力": {"img": "produce/rest.png", "keyword": ["体力"]},
        "交谈": {"img": "produce/chat.png", "keyword": ["先生に相談して"]}
    }

    EVENT_CONFIG = {
        "Da": "produce/Da.png",
        "Vi": "produce/Vi.png",
        "Vo": "produce/Vo.png",
        "交谈": "produce/chat.png",
        "上课": "produce/lesson.png",
        "活动": "produce/event.png",
        "外出": "produce/go_out.png"
    }

    # 阈值常量
    LOW_HEALTH_THRESHOLD = 0.3
    HIGH_POINT_THRESHOLD = 300
    CLICK_DELAY = 0.5
    ACTION_DELAY = 3.0

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        logger.success("事件: 选择事件")

        # 获取屏幕截图
        image = self._get_screenshot(context)

        # 1. 优先检查SP课程
        if self._handle_sp_course(context, image):
            return True

        # 2. 检查老师建议
        if self._handle_teacher_suggestion(context, image):
            return True

        # 3. 获取可用事件列表
        available_events = self._get_available_events(context, image)
        if not available_events:
            logger.info("无可用事件")
            return True

        # 4. 根据角色状态选择事件
        return self._choose_event_by_status(context, image, available_events, argv)

    @staticmethod
    def _get_screenshot(context: Context):
        """获取屏幕截图"""
        return context.tasker.controller.post_screencap().wait().get()

    def _handle_sp_course(self, context: Context, image) -> bool:
        """处理SP课程选择"""
        reco_detail = context.run_recognition(
            "ProduceChooseEventSp", image,
            pipeline_override={"ProduceChooseEventSp": {
                "recognition": "TemplateMatch",
                "template": "produce/sp.png",
                "roi": [0, 880, 720, 220]
            }}
        )

        if reco_detail and reco_detail.hit:
            logger.info("存在SP课程")
            result = reco_detail.best_result.box
            self._double_click(context, result[0] + 80, result[1] + 80)
            time.sleep(self.ACTION_DELAY)
            return True
        return False

    def _handle_teacher_suggestion(self, context: Context, image) -> bool:
        """处理老师建议"""
        ocr_keywords = self._get_ocr_keywords()

        reco_detail = context.run_recognition(
            "ProduceChooseEventSuggestion", image,
            pipeline_override={"ProduceChooseEventSuggestion": {
                "recognition": "OCR",
                "expected": ocr_keywords,
                "roi": [270, 160, 350, 56]
            }}
        )

        if not (reco_detail and reco_detail.hit):
            return False

        suggestion_text = "".join(item.text for item in reco_detail.filtered_results)
        logger.info(f"老师建议: {suggestion_text}")

        suggestion_img = self._find_image_from_phrase(suggestion_text, self.SUGGESTION_CONFIG)
        if not suggestion_img:
            return False

        logger.info(f"建议图片: {suggestion_img}")
        return self._execute_suggestion(context, image, suggestion_img)

    def _execute_suggestion(self, context: Context, image, suggestion_img: str) -> bool:
        """执行老师建议"""
        reco_detail = context.run_recognition(
            "ProduceChooseSuggestion", image,
            pipeline_override={"ProduceChooseSuggestion": {
                "recognition": "TemplateMatch",
                "template": suggestion_img,
                "roi": [0, 800, 720, 256]
            }}
        )

        if not (reco_detail and reco_detail.hit):
            return False

        logger.info("存在建议课程")
        result = reco_detail.best_result.box

        if "rest" in suggestion_img:
            logger.info("选择休息")
            context.run_task("ProduceChooseRest")
            time.sleep(self.ACTION_DELAY)
            return True

        if "chat" in suggestion_img:
            logger.info("选择交谈")
            self._double_click(context, result[0] + 80, result[1] + 80)
            context.run_task("ProduceShoppingFlag")
            time.sleep(self.ACTION_DELAY)
            return True

        # 其他建议课程
        self._double_click(context, result[0] + 80, result[1] + 80)
        time.sleep(self.ACTION_DELAY)
        return True

    def _get_available_events(self, context: Context, image) -> List[Dict[str, Any]]:
        """获取可用事件列表"""
        available_events = []
        available_events_name = ""

        for event_name, event_img in self.EVENT_CONFIG.items():
            reco_detail = context.run_recognition(
                "ProduceRecognitionEvent", image,
                pipeline_override={"ProduceRecognitionEvent": {
                    "recognition": "TemplateMatch",
                    "template": event_img,
                    "roi": [0, 880, 720, 220]
                }}
            )
            if reco_detail and reco_detail.hit:
                available_events.append({event_name: reco_detail.best_result.box})
                available_events_name += event_name

        logger.info(f"可用事件: {available_events_name}")
        return available_events

    def _choose_event_by_status(self, context: Context, image, available_events: List[Dict], argv) -> bool:
        """根据角色状态选择事件"""
        # 检查体力状态
        if self._handle_low_health(context, image, available_events):
            return True

        # 检查积分状态
        if self._handle_high_points(context, image, available_events):
            return True

        # 根据偏好选择事件
        return self._choose_by_preference(context, available_events, argv)

    def _handle_low_health(self, context: Context, image, available_events: List[Dict]) -> bool:
        """处理低体力情况"""
        health_ratio = self._get_health_ratio(context, image)
        if health_ratio is None or health_ratio >= self.LOW_HEALTH_THRESHOLD:
            return False

        logger.info("体力过低")

        # 优先选择外出
        go_out_box = self._get_event_box("外出", available_events)
        if go_out_box:
            logger.info("选择外出")
            self._double_click(context, go_out_box[0] + 80, go_out_box[1] + 80)
            time.sleep(self.ACTION_DELAY)
            return True

        # 其次选择休息
        reco_detail = context.run_recognition("ProduceChooseRest", image)
        if reco_detail.best_result:
            logger.info("选择休息")
            context.run_task("ProduceChooseRest")
            time.sleep(self.ACTION_DELAY)
            return True

        return False

    def _handle_high_points(self, context: Context, image, available_events: List[Dict]) -> bool:
        """处理高积分情况"""
        points = self._get_current_points(context, image)
        if points is None or points <= self.HIGH_POINT_THRESHOLD:
            return False

        logger.info(f"积分充足: {points}")
        chat_box = self._get_event_box("交谈", available_events)
        if chat_box:
            logger.info("选择交谈")
            self._double_click(context, chat_box[0] + 80, chat_box[1] + 80)
            context.run_task("ProduceShoppingFlag")
            time.sleep(self.ACTION_DELAY)
            return True

        return False

    def _choose_by_preference(self, context: Context, available_events: List[Dict], argv) -> bool:
        """根据偏好选择事件"""
        # 获取偏好设置
        preference = self._get_preference(argv)

        # 按优先级顺序尝试选择事件
        event_priority = [
            (preference, "偏好属性"),
            ("上课", "上课"),
            ("活动", "活动"),
            ("交谈", "交谈"),
            ("外出", "外出")
        ]

        for event_type, description in event_priority:
            event_box = self._get_event_box(event_type, available_events)
            if event_box:
                logger.info(f"选择{description}")
                self._double_click(context, event_box[0] + 80, event_box[1] + 80)

                if event_type == "交谈":
                    context.run_task("ProduceShoppingFlag")

                time.sleep(self.ACTION_DELAY)
                return True

        return False

    @staticmethod
    def _get_health_ratio(context: Context, image) -> Optional[float]:
        """获取体力比例"""
        reco_detail = context.run_recognition("ProduceRecognitionHealth", image)
        if not (reco_detail and reco_detail.hit):
            return None

        try:
            health_parts = reco_detail.best_result.text.split("/")
            current_health = int(health_parts[0])
            max_health = int(health_parts[1])
            ratio = current_health / max_health
            logger.info(f"体力: {current_health}/{max_health} ({ratio:.2%})")
            return ratio
        except (ValueError, IndexError, ZeroDivisionError):
            logger.warning("体力数据解析失败")
            return None

    @staticmethod
    def _get_current_points(context: Context, image) -> Optional[int]:
        """获取当前积分"""
        reco_detail = context.run_recognition("ProduceRecognitionPoint", image)
        if not (reco_detail and reco_detail.hit):
            return None

        try:
            points = int(reco_detail.best_result.text.replace(",", ""))
            logger.info(f"积分: {points}")
            return points
        except ValueError:
            logger.warning("积分数据解析失败")
            return None

    @staticmethod
    def _get_preference(argv) -> str:
        """获取用户偏好设置"""
        try:
            params = json.loads(argv.custom_action_param)
            preference = params.get("preference") if isinstance(params, dict) else None
            return preference if preference in ["Da", "Vi", "Vo"] else "Da"
        except (json.JSONDecodeError, AttributeError):
            return "Da"

    def _get_ocr_keywords(self) -> List[str]:
        """获取OCR关键词列表"""
        return [
            keyword
            for category_info in self.SUGGESTION_CONFIG.values()
            if "keyword" in category_info and isinstance(category_info["keyword"], list)
            for keyword in category_info["keyword"]
        ]

    def _double_click(self, context: Context, x: int, y: int):
        """执行双击操作"""
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()

    @staticmethod
    def _find_image_from_phrase(target_phrase: str, data_dict: Dict) -> Optional[str]:
        """
        检测给定的短语（句子）包含字典中哪个类别的关键词，并返回对应的图片路径。

        Args:
            target_phrase: 要查找的短语或句子
            data_dict: 包含分类信息的字典

        Returns:
            如果短语包含匹配的类别关键词，则返回对应的 'img' 值；否则返回 None
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
    def _get_event_box(event_name: str, event_list: List[Dict]) -> Optional[List]:
        """
        从事件列表中获取指定事件的坐标框

        Args:
            event_name: 事件名称
            event_list: 包含事件信息的字典列表

        Returns:
            如果找到事件，返回其坐标框；否则返回 None
        """
        if not event_list:
            return None

        for event_dict in event_list:
            if event_name in event_dict:
                return event_dict[event_name]
        return None


@AgentServer.custom_action("ProduceCardsAuto")
class ProduceCardsAuto(CustomAction):
    """
        自动识别根据系统提示出牌
        15秒未检测到提示牌，则打出最高分的牌
        识别不到体力退出函数
        处理是否打出该牌的弹窗
    """
    # 阈值常量
    CLICK_DELAY = 0.3
    ACTION_DELAY = 5.0

    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:

        start_time = time.time()
        while True:

            # 识别手牌
            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition('ProduceRecognitionMoveCards', image)
            if reco_detail.hit:
                # 处理移动卡片界面
                self._handle_move_cards(context)
                time.sleep(self.ACTION_DELAY)
                start_time = time.time()
                continue
            reco_detail = context.run_recognition("ProduceRecognitionCards", image)
            if reco_detail and reco_detail.hit:
                results = reco_detail.all_results

                label_counts = Counter()
                suggestions_box = [0, 0]
                best_box = [0, 0]
                best_score = 0
                for result in results:
                    label_counts[result.label] += 1
                    if result.label == "suggestions":
                        suggestions_box = result.box
                    if result.label == "cards":
                        if result.score > best_score:
                            best_score = result.score
                            best_box = result.box
                suggestions = label_counts["suggestions"]
                useless = label_counts["useless"]
                cards = label_counts["cards"]
                # print(f"卡片数量:{suggestions}/{cards}/{useless}")

                if suggestions > 0:

                    if suggestions_box[1] < 840 or suggestions_box[1] > 1150:
                        continue
                    context.tasker.controller.post_click(suggestions_box[0] + 100, suggestions_box[1] + 140).wait()
                    time.sleep(self.CLICK_DELAY)
                    context.tasker.controller.post_click(suggestions_box[0] + 100, suggestions_box[1] + 140).wait()
                    end_time = time.time()
                    logger.info("出牌 耗时:{:.2f}秒".format(end_time - start_time))
                    time.sleep(self.ACTION_DELAY)
                    start_time = time.time()
                elif useless > 0 and suggestions == 0 and cards == 0:
                    logger.warning("!!!!!!!!无可用牌!!!!!!!!!!!")
                    context.run_task("ProduceRecognitionSkipRound")
                    time.sleep(self.ACTION_DELAY)
                    start_time = time.time()

                end_time = time.time()
                if end_time - start_time > 15:
                    if best_box[1] < 840 or best_box[1] > 1150:
                        continue

                    reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", image)
                    if not reco_detail.hit:
                        # 应对极其稀有的但是遇到过一次的把考试结果识别成卡片的情况
                        logger.info("未检测到卡片和体力")
                        logger.success("事件: 退出出牌")
                        break
                    else:
                        logger.warning("检测超时")
                        context.tasker.controller.post_click(best_box[0], best_box[1]).wait()
                        time.sleep(self.CLICK_DELAY)
                        context.tasker.controller.post_click(best_box[0], best_box[1]).wait()
                        time.sleep(self.ACTION_DELAY)
                        start_time = time.time()

            else:
                reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", image)
                if not (reco_detail and reco_detail.hit):
                    logger.info("未检测到卡片和体力")
                    logger.success("事件: 退出出牌")
                    break

                reco_detail = context.run_recognition("ProduceYes", image)
                if reco_detail.best_result:
                    logger.info("点击确认")
                    context.tasker.controller.post_click(reco_detail.best_result.box[0], reco_detail.best_result.box[1]).wait()

                reco_detail = context.run_recognition("ProduceRecognitionNoCards", image)
                if reco_detail.best_result:
                    logger.info("无手牌")
                    context.run_task("ProduceRecognitionSkipRound")
                    time.sleep(self.ACTION_DELAY)

            if context.tasker.stopping:
                logger.info("任务中断")
                return True
            time.sleep(self.CLICK_DELAY)

        return True

    @staticmethod
    def _handle_move_cards(context: Context) -> bool:
        """
            一种处理移动卡片界面的笨方法
            在识别小卡片模型出来前可能都只能这样处理
        """
        y = 450
        while y < 1100:
            # for x in [140, 285, 440, 585]:
            for x in [140, 285]:
                context.tasker.controller.post_click(x, y).wait()
                time.sleep(0.2)
                context.tasker.controller.post_click(x, y).wait()
                time.sleep(0.2)
            image = context.tasker.controller.post_screencap().wait().get()
            reco_detail = context.run_recognition('ProduceRecognitionChooseMoveCards', image)
            if not reco_detail.hit:
                context.tasker.controller.post_click(360, 1160).wait()
                break
            else:
                y = y + 100
            if context.tasker.stopping:
                logger.info("任务中断")
                return True
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
                    "roi": [46, 479, 626, 529],
                    "pre_wait_freezes" : 100
                }})
        logger.success("事件: 商店购买")
        if reco_detail and reco_detail.hit:
            for result in reco_detail.filtered_results:
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
                if reco_detail.best_result:
                    logger.info("饮料已满，放弃购买")
                    continue
                reco_detail = context.run_recognition("ProduceShoppingBuy", image)
                if reco_detail and reco_detail.hit:
                    context.run_task("ProduceShoppingBuy")
                    start_time = time.time()
                    while True:
                        context.run_task("Click_1")
                        time.sleep(0.5)
                        end_time = time.time()
                        if end_time - start_time > 5:
                            break
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


@AgentServer.custom_action("ProduceKeepDrinkAuto")
class ProduceKeepDrinkAuto(CustomAction):
    """
        处理保留饮料的窗口
        原本是在ProduceButton节点直接按保留按钮处理
        但是需要处理只有2瓶饮料时，一次性获得2瓶饮料时不会自动勾选3瓶的特殊情况
        所以转为独立处理
    """
    def run(
            self,
            context: Context,
            argv: CustomAction.RunArg,
    ) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("ProduceRecognitionUncheckedMark", image)
        if reco_detail.hit:
            for result in reco_detail.filtered_results:
                box = result.box
                context.tasker.controller.post_click(box[0]+int(box[2]/2), box[1]+int(box[3]/2)).wait()
                time.sleep(0.1)
            context.run_task("ProduceDrinkNoButton")
        return True
