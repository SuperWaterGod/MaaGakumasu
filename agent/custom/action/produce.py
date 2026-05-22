import json
import time
import random
import itertools
from typing import Any, Dict, List, Optional
from collections import Counter

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer


@AgentServer.custom_action("ProduceChooseEventAuto")
class ProduceChooseEventAuto(CustomAction):
    """
    自动识别选择培育事件
    优先根据老师建议选择，没有建议则选择SP
    然后检测体力和点数
    最后根据偏好设置选择事件
    """

    # 常量定义
    SUGGESTION_CONFIG = {
        "Vo": {"img": "produce/Vo.png", "keyword": ["ボーカル", "唱歌"]},
        "Da": {"img": "produce/Da.png", "keyword": ["ダンス", "舞蹈"]},
        "Vi": {"img": "produce/Vi.png", "keyword": ["ビジュアル", "视觉"]},
        "体力": {"img": "produce/rest.png", "keyword": ["体力"]},
        "交谈": {"img": "produce/chat.png", "keyword": ["先生に相談して", "咨询"]},
    }

    EVENT_CONFIG = {
        "Da": "produce/Da.png",
        "Vi": "produce/Vi.png",
        "Vo": "produce/Vo.png",
        "交谈": "produce/chat.png",
        "上课": "produce/lesson.png",
        "活动": "produce/event.png",
        "外出": "produce/go_out.png",
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

        # 1. 优先检查老师建议
        if self._handle_teacher_suggestion(context, image):
            return True

        # 2. 检查SP课程
        if self._handle_sp_course(context, image):
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
            "ProduceChooseEventSp",
            image,
            pipeline_override={
                "ProduceChooseEventSp": {"recognition": "TemplateMatch", "template": "produce/sp.png", "roi": [0, 880, 720, 220]}
            },
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
            "ProduceChooseEventSuggestion",
            image,
            pipeline_override={"ProduceChooseEventSuggestion": {"recognition": "OCR", "expected": ocr_keywords, "roi": [270, 160, 350, 56]}},
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
            "ProduceChooseSuggestion",
            image,
            pipeline_override={
                "ProduceChooseSuggestion": {"recognition": "TemplateMatch", "template": suggestion_img, "roi": [0, 800, 720, 256]}
            },
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
                "ProduceRecognitionEvent",
                image,
                pipeline_override={
                    "ProduceRecognitionEvent": {"recognition": "TemplateMatch", "template": event_img, "roi": [0, 880, 720, 220]}
                },
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
            time.sleep(self.ACTION_DELAY)
            return True

        return False

    def _choose_by_preference(self, context: Context, available_events: List[Dict], argv) -> bool:
        """根据偏好选择事件"""
        # 获取偏好设置
        preference = self._get_preference(argv)

        # 按优先级顺序尝试选择事件
        event_priority = [(preference, "偏好属性"), ("上课", "上课"), ("活动", "活动"), ("交谈", "交谈"), ("外出", "外出")]

        for event_type, description in event_priority:
            event_box = self._get_event_box(event_type, available_events)
            if event_box:
                logger.info(f"选择{description}")
                self._double_click(context, event_box[0] + 80, event_box[1] + 80)

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


@AgentServer.custom_action("ProduceChooseNIAEventAuto")
class ProduceChooseNIAEventAuto(CustomAction):
    """
    自动选择NIA培育事件。

    选择优先级顺序（从高到低）：
    1. 低体力保护：体力 < 20% 或体力值 < 8时，优先外出恢复体力，其次休息
    2. 老师建议：根据OCR结果匹配Vo/Da/Vi属性关键词
    3. SP课程（第一属性，仅当该属性分数占比 < 80%时）
    4. SP课程（第二属性，仅当该属性分数占比 < 80%时）
    5. 营业：获得score
    6. 活动：获得points
    7. 第一属性课程（仅当该属性分数占比 < 80%时）
    8. 第二属性课程（仅当第一属性分数占比 >= 85%时，且第二属性占比 < 80%）
    9. 外出：恢复体力
    10. 指导：点击后需执行ProduceGuideEntry任务
    11. SP课程（非第一/第二属性）
    12. 商店：点击后需执行ProduceShoppingEntry任务

    事件数据结构：
    - 属性事件：{"Vo": [x,y,w,h], "SP": bool}，SP为True表示该属性有SP课程
    - 其他事件：{"事件名称": [x,y,w,h]}
    """

    # 常量定义
    SUGGESTION_CONFIG = {
        "Vo": {"img": "produce/NIA/Vo.png", "keyword": ["ボーカル", "唱歌"]},
        "Da": {"img": "produce/NIA/Da.png", "keyword": ["ダンス", "舞蹈"]},
        "Vi": {"img": "produce/NIA/Vi.png", "keyword": ["ビジュアル", "视觉"]},
        "体力": {"img": "produce/rest.png", "keyword": ["体力"]},
        "指导": {"img": "produce/NIA/guide.png", "keyword": ["特别指導", "特别指导"]},
        "交谈": {"img": "produce/NIA/chat.png", "keyword": ["相談", "咨询"]},
    }

    EVENT_CONFIG = {
        "Vo": "produce/NIA/Vo.png",
        "Da": "produce/NIA/Da.png",
        "Vi": "produce/NIA/Vi.png",
        "交谈": "produce/NIA/chat.png",
        "活动": "produce/NIA/activity.png",
        "指导": "produce/NIA/guide.png",
        "外出": "produce/NIA/go_out.png",
        "工作": "produce/NIA/work.png",
    }

    # 阈值常量
    CLICK_DELAY = 0.5
    ACTION_DELAY = 3.0
    PREFERENCE_LIST = ["Da", "Vi", "Vo"]
    FIRST_NEAR_FULL_RATIO = 0.85
    ATTR_STOP_RATIO = 0.8
    LOW_HEALTH_RATIO = 0.2
    LOW_HEALTH_VALUE = 8

    def __init__(self):
        super().__init__()
        self.first = "Vi"
        self.second = "Da"

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        logger.success("事件: 选择事件")

        preference = self._get_preference(argv)
        self.first = preference["first"]
        self.second = preference["second"]
        logger.info(f"第一属性: {self.first}, 第二属性: {self.second}")

        # 获取屏幕截图
        image = self._get_screenshot(context)

        suggestion = ""
        if context.get_node_data("ProduceSuggestion").get("enabled", True):
            suggestion = self._get_suggestion(context, image)

        health_data = self._get_health(context, image) or {"current": 34, "max": 34, "ratio": 1.0}
        points = self._get_current_points(context, image) or 0
        score = self._get_current_score(context, image) or {"Vo": 0, "Da": 0, "Vi": 0, "max": 1}
        events = self._get_available_events(context, image)

        # 选择最佳事件
        best_event = self._choose_best_event(suggestion, health_data, points, score, events)
        if not best_event:
            logger.info("无可用事件")
            return True

        logger.info(f"选择事件: {best_event['name']}, 坐标: ({best_event['box'][0]}, {best_event['box'][1]})")

        # 执行事件
        return self._execute_event(context, best_event)

    def _choose_best_event(self, suggestion: str, health_data: dict, points: int, score: dict, events: list) -> Optional[dict]:
        """
        根据获取到的信息从可用事件中选择最佳事件。

        Returns:
            dict: {"name": str, "box": [x, y, w, h], "run_task": str}，None表示无事件可选
        """
        max_score = score.get("max", 1)
        first_ratio = score.get(self.first, 0) / max_score if max_score > 0 else 0
        second_ratio = score.get(self.second, 0) / max_score if max_score > 0 else 0
        is_first_near_full = first_ratio >= self.FIRST_NEAR_FULL_RATIO
        is_first_stopped = first_ratio >= self.ATTR_STOP_RATIO
        is_second_stopped = second_ratio >= self.ATTR_STOP_RATIO

        # 0. 低体力处理
        current_health = health_data["current"] if health_data else 0
        ratio_health = health_data["ratio"] if health_data else 1.0
        if current_health < self.LOW_HEALTH_VALUE or ratio_health < self.LOW_HEALTH_RATIO:
            go_out = self._find_event_by_name(events, "外出")
            if go_out and points >= 100:
                return self._make_event("外出", go_out)
            logger.info("体力过低，选择休息")
            return {"name": "rest", "box": [0, 0, 0, 0], "run_task": "ProduceChooseRest"}

        # 1. 老师建议
        suggestion_attr = self._parse_suggestion(suggestion)
        if suggestion_attr:
            event = self._find_attr_event(events, suggestion_attr)
            if event:
                return self._make_event(suggestion_attr, event)

        # 2. SP（第一属性，仅当属性未达80%时）
        if not is_first_stopped:
            event = self._find_attr_event(events, self.first, need_sp=True)
            if event:
                return self._make_event(f"{self.first}_SP", event)

        # 3. SP（第二属性，仅当属性未达80%时）
        if not is_second_stopped:
            event = self._find_attr_event(events, self.second, need_sp=True)
            if event:
                return self._make_event(f"{self.second}_SP", event)

        # 4. 营业
        event = self._find_event_by_name(events, "工作")
        if event:
            return self._make_event("工作", event, run_task="ProduceWorkEntry")

        # 5. 活动
        event = self._find_event_by_name(events, "活动")
        if event:
            return self._make_event("活动", event)

        # 6. 第一属性课程（属性未达80%时）
        if not is_first_stopped:
            event = self._find_attr_event(events, self.first, need_sp=False)
            if event:
                return self._make_event(self.first, event)

        # 7. 第二属性课程（仅当第一属性快满时，且第二属性未达80%）
        if is_first_near_full and not is_second_stopped:
            event = self._find_attr_event(events, self.second, need_sp=False)
            if event:
                return self._make_event(self.second, event)

        # 8. 外出
        event = self._find_event_by_name(events, "外出")
        if event:
            return self._make_event("外出", event)

        # 9. 指导（点击后需执行任务）
        event = self._find_event_by_name(events, "指导")
        if event:
            return self._make_event("指导", event, run_task="ProduceGuideEntry")

        # 10. SP（其他属性）
        for attr in ["Vo", "Da", "Vi"]:
            if attr not in [self.first, self.second]:
                event = self._find_attr_event(events, attr, need_sp=True)
                if event:
                    return self._make_event(f"{attr}_SP", event)

        # 11. 商店（点击后需执行任务）
        event = self._find_event_by_name(events, "商店")
        if event:
            return self._make_event("商店", event, run_task="ProduceShoppingEntry")

        return None

    def _execute_event(self, context: Context, event: dict) -> bool:
        """执行事件：双击坐标，等待动画后执行后续任务（如果有）。"""
        run_task = event.get("run_task")
        box = event["box"]

        x = box[0] + box[2] // 2
        y = box[1] + box[3] // 2
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.ACTION_DELAY)
        if run_task:
            logger.info(f"执行任务{run_task}")
            context.run_task(run_task)
        return True

    def _find_attr_event(self, events: list, attr: str, need_sp: bool = False) -> Optional[list]:
        """查找指定属性事件的坐标。need_sp=True时只返回有SP标记的属性事件。"""
        for event in events:
            if attr in event:
                if need_sp and not event.get("SP"):
                    continue
                return event[attr]
        return None

    def _find_event_by_name(self, events: list, event_name: str) -> Optional[list]:
        """从事件列表中查找指定名称事件的坐标。"""
        for event in events:
            for key in event:
                if event_name in key:
                    return event[key]
        return None

    def _make_event(self, name: str, box: list, run_task: str = "") -> dict:
        """创建事件字典。"""
        return {"name": name, "box": box, "run_task": run_task}

    def _parse_suggestion(self, suggestion: str) -> Optional[str]:
        """从老师建议中解析属性名称，匹配SUGGESTION_CONFIG中的关键词。"""
        if not suggestion:
            return None
        for attr in ["Vo", "Da", "Vi"]:
            for keyword in self.SUGGESTION_CONFIG[attr].get("keyword", []):
                if keyword in suggestion:
                    return attr
        return None

    @staticmethod
    def _get_screenshot(context: Context):
        """获取屏幕截图"""
        return context.tasker.controller.post_screencap().wait().get()

    def _get_preference(self, argv) -> dict:
        """获取偏好设置"""
        try:
            params = json.loads(argv.custom_action_param)
            first = params.get("first") if isinstance(params, dict) else None
            second = params.get("second") if isinstance(params, dict) else None
            return {"first": first if first in self.PREFERENCE_LIST else "Vi", "second": second if second in self.PREFERENCE_LIST else "Da"}
        except (json.JSONDecodeError, AttributeError):
            logger.warning("偏好设置解析失败，使用默认值")
            return {"first": "Vi", "second": "Da"}

    def _get_suggestion(self, context: Context, image) -> str:
        """获取建议课程"""
        reco_detail = context.run_recognition(
            "ProduceChooseEventSuggestion",
            image,
            pipeline_override={"ProduceChooseEventSuggestion": {"recognition": "OCR", "roi": [270, 170, 350, 80]}},
        )

        if not (reco_detail and reco_detail.hit):
            return ""

        suggestion_text = "".join(item.text for item in reco_detail.filtered_results)
        logger.info(f"老师建议: {suggestion_text}")
        return suggestion_text

    @staticmethod
    def _get_health(context: Context, image) -> Optional[dict]:
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
            return {"current": current_health, "max": max_health, "ratio": ratio}
        except (ValueError, IndexError, ZeroDivisionError):
            logger.warning("体力数据解析失败")
            return None

    @staticmethod
    def _get_current_points(context: Context, image) -> Optional[int]:
        """获取当前积分"""
        reco_detail = context.run_recognition(
            "ProduceRecognitionPoint",
            image,
            pipeline_override={"ProduceRecognitionPoint": {"roi": [320, 90, 130, 54]}},
        )
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
    def _get_current_score(context: Context, image) -> Optional[dict]:
        """获取当前得分"""
        score = {
            "Vo": 0,
            "Da": 0,
            "Vi": 0,
            "max": 0,
        }
        for i in range(3):
            reco_detail = context.run_recognition(
                "ProduceRecognitionScore",
                image,
                pipeline_override={"ProduceRecognitionScore": {"roi": [150 + i * 150, 668, 136, 80]}},
            )
            if reco_detail and reco_detail.hit and len(reco_detail.filtered_results) == 2:
                current_score = int("".join(filter(str.isdigit, reco_detail.filtered_results[0].text)))
                max_score = int("".join(filter(str.isdigit, reco_detail.filtered_results[1].text.replace("/", ""))))
                logger.debug(f"第{i + 1}列得分: {current_score} / {max_score}")
                score[next(itertools.islice(score.keys(), i, None))] = current_score
                score["max"] = max_score if score["max"] < max_score < 9999 else score["max"]
        try:
            logger.info(f"当前得分: Vo={score['Vo']}, Da={score['Da']}, Vi={score['Vi']}, Max={score['max']}")
            return score
        except ValueError:
            logger.warning("积分数据解析失败")
            return None

    def _get_available_events(self, context: Context, image) -> List[Dict[str, Any]]:
        """获取可用事件列表"""
        available_events = []
        available_events_name = ""

        for event_name, event_img in self.EVENT_CONFIG.items():
            reco_detail = context.run_recognition(
                "ProduceRecognitionEvent",
                image,
                pipeline_override={
                    "ProduceRecognitionEvent": {"recognition": "TemplateMatch", "template": event_img, "roi": [0, 880, 720, 220]}
                },
            )
            if reco_detail and reco_detail.hit:
                if event_name in ["Da", "Vi", "Vo"]:
                    available_events_name = "Vo, Da, Vi"
                    available_events = [
                        {"Vo": [190, 1000, 1, 1], "SP": self._get_sp_course(context, image, [70, 900, 80, 80])},
                        {"Da": [360, 1000, 1, 1], "SP": self._get_sp_course(context, image, [250, 900, 80, 80])},
                        {"Vi": [530, 1000, 1, 1], "SP": self._get_sp_course(context, image, [430, 900, 80, 80])},
                    ]
                    break
                available_events.append({event_name: reco_detail.best_result.box})
                available_events_name += f"{event_name}, "

        logger.info(f"可用事件: {available_events_name.rstrip(', ')}")
        logger.debug(available_events)
        return available_events

    def _get_sp_course(self, context: Context, image, sp_roi: List[int]) -> bool:
        """获取SP课程选择"""
        reco_detail = context.run_recognition(
            "ProduceChooseEventSp",
            image,
            pipeline_override={"ProduceChooseEventSp": {"recognition": "TemplateMatch", "template": "produce/sp.png", "roi": sp_roi}},
        )
        if reco_detail and reco_detail.hit:
            logger.debug(f"{sp_roi}存在SP课程")
            return True
        return False


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
    TIME_OUT = 15.0

    def __init__(self):
        super().__init__()
        self.start_time = time.time()

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        # 使用饮料
        self._wait_until_playable(context)
        image = context.tasker.controller.post_screencap().wait().get()
        reco_detail = context.run_recognition("ProduceCheckDrinkButton", image)
        hit_count = len(reco_detail.filtered_results)  # 获取饮料数
        logger.info(f"识别到{hit_count}瓶饮料")
        for _ in range(hit_count):
            context.run_task("ProduceUseDrink")
            self._wait_until_playable(context, 2)

        # 开始出牌
        self.start_time = time.time()
        while True:
            # 处理手动终止任务
            if context.tasker.stopping:
                logger.info("任务中断")
                return True

            # 截图
            image = context.tasker.controller.post_screencap().wait().get()

            # 通过检测体力槽判断是否处于出牌场景
            if not self._is_playing_card(context, image):
                logger.info("未检测到体力")
                logger.success("事件: 退出出牌")
                break

            # 识别手牌
            reco_detail = context.run_recognition("ProduceRecognitionCards", image)
            if reco_detail and reco_detail.hit:
                # 目前模型识别的准确度不够高，暂时使用all_results
                results = reco_detail.all_results

                # 获取卡牌信息
                suggestions, useless, cards, suggestions_box, best_box = self._get_card_info(results)
                # print(f"卡片数量:{suggestions}/{cards}/{useless}")

                # 有推荐牌时，打出推荐牌
                if suggestions > 0:
                    if suggestions_box[1] < 840 or suggestions_box[1] > 1150:
                        continue
                    self._play_a_card(context, suggestions_box)
                # 只有一张可用牌时，直接打出该牌
                elif cards == 1:
                    if best_box[1] < 840 or best_box[1] > 1150:
                        continue
                    time.sleep(1)  # 防止点击过早导致只命中一次
                    self._play_a_card(context, best_box)
                # 没有可用牌时，先判断是否处于出牌场景，确认处于出牌场景后，再跳过回合
                elif useless > 0 and suggestions == 0 and cards == 0:
                    logger.warning("!!!!!!!!无可用牌!!!!!!!!!!!")
                    context.run_task("ProduceRecognitionSkipRound")
                    self._wait_until_playable(context)
                    self.start_time = time.time()

                end_time = time.time()
                if end_time - self.start_time > self.TIME_OUT:
                    if best_box[1] < 840 or best_box[1] > 1150:
                        continue

                    logger.warning("检测超时")
                    self._play_a_card(context, best_box)

            else:
                reco_detail = context.run_recognition("ProduceRecognitionNoCards", image)
                if reco_detail.hit:
                    logger.info("无手牌")
                    context.run_task("ProduceRecognitionSkipRound")
                    self._wait_until_playable(context)
                    self.start_time = time.time()

            time.sleep(self.CLICK_DELAY)

        return True

    @staticmethod
    def _get_card_info(results: list):
        """
        从识别结果中获取卡牌信息

        Args:
            results (list): 识别结果列表

        Returns:
            suggestions (int): 建议牌数量
            useless (int): 无用牌数量
            cards (int): 可用牌数量
            suggestions_box (list): 建议牌区域，格式为[x, y, w, h]（x、y为区域左上角的坐标）
            best_box (list): 可用牌区域，格式为[x, y, w, h]（x、y为区域左上角的坐标）
        """
        label_counts = Counter()
        suggestions_box = [0, 0, 1, 1]
        best_box = [0, 0, 1, 1]
        best_score = 0
        for result in results:
            label_counts[result.label] += 1
            if result.label == "suggestions":
                suggestions_box = result.box
            if result.label == "cards":
                # 选出识别分数最高分的卡牌，避免错误
                if result.score > best_score:
                    best_score = result.score
                    best_box = result.box
        suggestions = label_counts["suggestions"]
        useless = label_counts["useless"]
        cards = label_counts["cards"]
        return suggestions, useless, cards, suggestions_box, best_box

    def _play_a_card(self, context: Context, box: list) -> bool:
        """
        出牌并处理移动卡牌界面

        Args:
            context: maa的Context类
            box: 点击范围，格式为[x, y, w, h]（x、y为点击范围左上角的坐标）

        Returns:
            bool: 如果执行没有问题，返回True；否则返回False。
        """

        # 出牌
        # context.tasker.controller.post_click(box[0] + 100, box[1] + 140).wait()
        context.tasker.controller.post_click(box[0] + box[2] // 2, box[1] + box[3] // 2).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(box[0] + box[2] // 2, box[1] + box[3] // 2).wait()
        logger.info("出牌 耗时:{:.2f}秒".format(time.time() - self.start_time))

        # 等待回到可出牌状态后，重置计时
        time.sleep(1)
        self._wait_until_playable(context)
        self.start_time = time.time()

        return True

    @staticmethod
    def _handle_move_cards(context: Context, image=None) -> bool:
        """
        一种处理移动卡片界面的笨方法

        Args:
            context: maa的Context类
            image: 截图

        Returns:
            bool: 如果出现了移动卡牌界面并处理成功，返回True；
                如果没有出现移动卡牌界面或处理过程出现问题，返回False。
        """
        if image is None:
            image = context.tasker.controller.post_screencap().wait().get()

        reco_detail = context.run_recognition("ProduceRecognitionChooseMoveCards", image)
        if reco_detail.hit:
            y = 450
            while y < 1100:
                # for x in [140, 285, 440, 585]:
                for x in [140, 285]:
                    context.tasker.controller.post_click(x, y).wait()
                    time.sleep(0.2)
                    context.tasker.controller.post_click(x, y).wait()
                    time.sleep(0.2)
                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition("ProduceRecognitionChooseMoveCards", image)
                if not reco_detail.hit:
                    context.run_task("ProduceMoveCards")
                    return True
                else:
                    y = y + 100
                # 检测任务中止的情况，防止卡死，检测成功时返回False
                if context.tasker.stopping:
                    return False
        return False

    @staticmethod
    def _is_playing_card(context: Context, image=None) -> bool:
        """
        判断是否处于出牌场景

        Args:
            context: maa的Context类
            image: 截图

        Returns:
            bool: 如果处于出牌场景，返回True；否则返回False。
        """
        if image is None:
            image = context.tasker.controller.post_screencap().wait().get()

        reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", image)
        if reco_detail and reco_detail.hit:
            return True
        # reco_detail = context.run_recognition('ProduceRecognitionChooseMoveCards', image)
        # if reco_detail and reco_detail.hit:
        #     return True
        return False

    def _wait_until_playable(self, context: Context, confirmation_count=1):
        """
        等待直到处于可出牌状态

        Args:
            context: maa的Context类
            confirmation_count：重复核对的次数，用来应对识别对象一闪而过的假True情况（主要存在于喝饮料的时候）

        Returns:
            bool: 处于出牌场景时，返回True；不处于出牌场景时，返回False
        """
        count_playable = 0
        count_exit = 0
        while True:
            image = context.tasker.controller.post_screencap().wait().get()

            # 通过跳过回合按钮检测是否处于可出牌状态
            reco_detail = context.run_recognition("ProduceRecognitionSkipRound", image)
            if reco_detail and reco_detail.hit:
                count_playable += 1
                if count_playable >= confirmation_count:
                    return True

            # 检测血条是否存在，如连续n次检查不到血条，则认为已退出出牌场景
            # 如果识别时间太长，2次就够了，主要避免CLEAR效果遮住血条的情况
            # 如果识别时间太短，导致提前出函数，CLEAR转PERFECT的那一回合出牌计时会差很远。如果出现这种情况，就设置为3次
            reco_detail = context.run_recognition("ProduceRecognitionHealthFlag", image)
            if not (reco_detail and reco_detail.hit):
                count_exit += 1
                if count_exit >= 2:
                    return False

            # 解决莫名其妙的误触问题
            reco_detail = context.run_recognition("ProduceButton", image)
            if reco_detail and reco_detail.hit:
                context.run_task("ProduceButton")

            # 处理移动卡片界面
            if self._handle_move_cards(context, image):
                count_playable = 0
                count_exit = 0

            # 检测任务中止的情况，防止卡死，检测成功时返回False
            if context.tasker.stopping:
                return False

            # 睡一会
            time.sleep(1)


@AgentServer.custom_action("ProduceChooseWorkAuto")
class ProduceChooseWorkAuto(CustomAction):
    """
    处理选择工作类型的窗口

    选择逻辑：
    - 体力 > 10 时：选择会扣除体力对应的工作（health_position 指定的位置）
      - 单个位置 [2] 或 [3]：选择对应的工作
      - 无位置或多位置 [2,3]：随机选择
    - 体力 <= 10 时：选择不扣除体力对应的工作
      - [2] → 选择第三个（位置3不扣体力）
      - [3] → 选择第二个（位置2不扣体力）
      - 其他情况 → 选择第一个
    """

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        first_roi = [100, 760]
        second_roi = [100, 880]
        third_roi = [100, 1000]
        image = context.tasker.controller.post_screencap().wait().get()
        health_data = self._get_health(context, image)
        current = health_data["current"] if health_data else 0

        health_position = self._get_health_position(context, image)

        if current > 10:
            if health_position and len(health_position) == 1:
                box = second_roi if health_position[0] == 2 else third_roi
            else:
                box = random.choice([first_roi, second_roi, third_roi])
        else:
            if health_position == [2]:
                box = third_roi
            elif health_position == [3]:
                box = second_roi
            else:
                box = first_roi
        context.tasker.controller.post_click(box[0], box[1]).wait()
        return True

    @staticmethod
    def _get_health(context: Context, image) -> Optional[dict]:
        """获取当前体力数值"""
        reco_detail = context.run_recognition("ProduceRecognitionHealth", image)
        if not (reco_detail and reco_detail.hit):
            return None

        try:
            health_parts = reco_detail.best_result.text.split("/")
            current_health = int(health_parts[0])
            max_health = int(health_parts[1])
            ratio = current_health / max_health
            logger.info(f"体力: {current_health}/{max_health} ({ratio:.2%})")
            return {"current": current_health, "max": max_health, "ratio": ratio}
        except (ValueError, IndexError, ZeroDivisionError):
            logger.warning("体力数据解析失败")
            return None

    @staticmethod
    def _get_health_position(context: Context, image) -> Optional[list]:
        """
        获取扣除体力的图标位置

        Returns:
            None: 未检测到扣体力图标
            [2]: 第二个位置扣体力
            [3]: 第三个位置扣体力
            [2,3]: 第二和第三个位置都扣体力
        """
        reco_detail = context.run_recognition(
            "ProduceRecognitionHealthFlag",
            image,
            pipeline_override={"ProduceRecognitionHealthFlag": {"roi": [370, 830, 320, 290]}},
        )
        if reco_detail.hit:
            position = []
            for result in reco_detail.filtered_results:
                health_box_y = result.box[1]
                if health_box_y > 970:
                    position.append(3)
                else:
                    position.append(2)
            return position
        return None


@AgentServer.custom_action("ProduceChooseOptionsAuto")
class ProduceChooseOptionsAuto(CustomAction):
    """
    处理选择选项的窗口

    根据当前得分自动选择属性选项：
    1. 获取第一、第二、第三属性的当前分数
    2. 70% 概率选择第三属性，30% 概率选择第二属性
    3. 在可用选项中执行双击选择
    4. 若目标选项不可用，按优先级 fallback：null > 第二属性 > 第三属性 > 随机
    """

    OPTIONS_CONFIG = {
        "Vo": "produce/choose_Vo.png",
        "Da": "produce/choose_Da.png",
        "Vi": "produce/choose_Vi.png",
        "null": "produce/choose_null.png",
    }
    CLICK_DELAY = 0.5

    def __init__(self):
        super().__init__()
        self.first = "Vi"
        self.second = "Da"

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        """
        执行自动选择选项

        Returns:
            bool: 选择成功返回 True，否则返回 False
        """
        preference = (
            context.get_node_data("ProduceChooseNIAEventFlag")
            .get("action", {})
            .get("param", {})
            .get("custom_action_param", {"first": "Vo", "second": "Vi"})
        )
        self.first = preference["first"]
        self.second = preference["second"]
        image = context.tasker.controller.post_screencap().wait().get()
        score = self._get_current_score(context, image) or {"Vo": 0, "Da": 0, "Vi": 0, "max": 1}
        options = self._get_available_options(context, image)

        # 计算选择
        first_score = score.get(self.first, 0)
        second_score = score.get(self.second, 0)
        # 计算第三属性（Vo、Da、Vi 中非 first/second 的那个）
        all_attrs = ["Vo", "Da", "Vi"]
        third = next(attr for attr in all_attrs if attr != self.first and attr != self.second)
        third_score = score.get(third, 0)
        logger.info(f"第一属性 {self.first}={first_score}, 第二属性 {self.second}={second_score}, 第三属性 {third}={third_score}")

        # 判断逻辑：70% 概率选择第三属性，30% 概率选择第二属性
        if random.random() < 0.7:
            choice = third
            logger.debug(f"随机数 < 0.7，选择第三属性: {choice}")
        else:
            choice = self.second
            logger.debug(f"随机数 >= 0.7，选择第二属性: {choice}")

        # 找到目标选项
        target_box = None
        for opt in options:
            if choice in opt:
                target_box = opt[choice]
                break

        # 如果目标选项不可用，尝试 fallback（null 优先）
        if target_box is None:
            logger.debug(f"选项 {choice} 不可用，尝试 fallback")
            # 按 null > 第二属性 > 第三属性 > 随机的优先级选择
            priority = ["null", self.second, third, None]
            for p in priority:
                if p is None:
                    # 最后从所有可用选项中随机选择
                    if options:
                        random_opt = random.choice(options)
                        choice = next(iter(random_opt.keys()))
                        target_box = random_opt[choice]
                        logger.debug(f"Fallback 随机选择: {choice}")
                    break
                for opt in options:
                    if p in opt:
                        target_box = opt[p]
                        choice = p
                        logger.debug(f"Fallback 选择 {p}")
                        break
                if target_box:
                    break

        # 执行双击
        if target_box:
            # box 格式为 [x, y, w, h]，计算中心点
            center_x = target_box[0] + target_box[2] // 2
            center_y = target_box[1] + target_box[3] // 2
            self._double_click(context, center_x, center_y)
            logger.info(f"已选择选项 {choice} at ({center_x}, {center_y})")
        else:
            logger.warning("没有可用选项，无法选择")
            return True  # 没有选项可选时默认返回True，避免卡死在这里
        return True

    @staticmethod
    def _get_current_score(context: Context, image) -> Optional[dict]:
        """获取当前得分"""
        score = {
            "Vo": 0,
            "Da": 0,
            "Vi": 0,
            "max": 0,
        }
        roi0_list = [[70 + i * 230, 430, 136, 80] for i in range(3)]
        roi1_list = [[150 + i * 150, 325, 136, 80] for i in range(3)]
        first_reco = context.run_recognition(
            "ProduceRecognitionScore",
            image,
            pipeline_override={"ProduceRecognitionScore": {"roi": roi0_list[0]}},
        )
        use_roi_list = roi1_list if not (first_reco and first_reco.hit) else roi0_list

        for i in range(3):
            reco_detail = context.run_recognition(
                "ProduceRecognitionScore",
                image,
                pipeline_override={"ProduceRecognitionScore": {"roi": use_roi_list[i]}},
            )
            if reco_detail and reco_detail.hit and len(reco_detail.filtered_results) == 2:
                current_score = int("".join(filter(str.isdigit, reco_detail.filtered_results[0].text)))
                max_score = int("".join(filter(str.isdigit, reco_detail.filtered_results[1].text.replace("/", ""))))
                logger.debug(f"第{i + 1}列得分: {current_score} / {max_score}")
                score[next(itertools.islice(score.keys(), i, None))] = current_score
                score["max"] = max_score if score["max"] < max_score < 9999 else score["max"]
        try:
            logger.info(f"当前得分: Vo={score['Vo']}, Da={score['Da']}, Vi={score['Vi']}, Max={score['max']}")
            return score
        except ValueError:
            logger.warning("积分数据解析失败")
            return None

    def _get_available_options(self, context: Context, image) -> List[Dict[str, Any]]:
        """获取可用选项列表"""
        available_options = []
        available_options_name = ""

        for option_name, option_img in self.OPTIONS_CONFIG.items():
            reco_detail = context.run_recognition(
                "ProduceRecognitionWorkOptions",
                image,
                pipeline_override={"ProduceRecognitionWorkOptions": {"template": option_img, "focus": None}},
            )
            if reco_detail and reco_detail.hit:
                available_options.append({option_name: reco_detail.best_result.box})
                available_options_name += f"{option_name}, "

        logger.info(f"可用选项: {available_options_name.rstrip(', ')}")
        return available_options

    def _double_click(self, context: Context, x: int, y: int):
        """执行双击操作"""
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()


@AgentServer.custom_action("ProduceChooseMirrorAuto")
class ProduceChooseMirrorAuto(CustomAction):
    """
    自动选择镜像挑战难度

    根据当前投票数在对应镜的阈值列表中选择合适的难度选项：
    - 获取当前镜（第一/第二/第三镜）
    - 获取当前投票数
    - 遍历阈值列表，找到第一个大于投票数的门槛，选择对应的选项
    - 若投票数低于所有阈值（threshold=0），则选择"无"选项
    - 点击前检测目标分数旁是否有锁定图标，若有则自动降档

    阈值配置（mirror）：
    - first:  [0, 4000, 9000]       -> 投票<4000选索引0，<9000选索引1，>=9000选索引2
    - second: [0, 14000, 25000]     -> 投票<14000选索引0，<25000选索引1，>=25000选索引2
    - third:  [0, 28000, 40000, 57000] -> 投票<28000选索引0，<40000选索引1，<57000选索引2，>=57000选索引3

    识别失败时回退到"无"选项
    """

    CLICK_DELAY = 0.5
    mirror = {"first": [0, 4000, 9000], "second": [0, 14000, 25000], "third": [0, 28000, 40000, 57000]}

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        image = context.tasker.controller.post_screencap().wait().get()
        vote = self._get_current_vote(context, image) or 1
        current_mirror, none_box = self._get_current_mirror(context, image) or ("first", [360, 1050, 1, 1])

        thresholds = self.mirror.get(current_mirror, [0])
        option_idx = 0
        for i, threshold in enumerate(thresholds):
            if vote >= threshold:
                option_idx = i

        while option_idx >= 0:
            target_threshold = thresholds[option_idx]
            if target_threshold == 0:
                x = none_box[0] + none_box[2] // 2
                y = none_box[1] + none_box[3] // 2
                break

            expected = f".*{target_threshold:,}.*"
            reco_detail = context.run_recognition(
                "ProduceRecognitionMirror",
                image,
                pipeline_override={"ProduceRecognitionMirror": {"expected": expected}},
            )
            if not reco_detail or not reco_detail.hit:
                logger.warning(f"未识别到目标分数: {target_threshold:,}")
                x = none_box[0] + none_box[2] // 2
                y = none_box[1] + none_box[3] // 2
                break

            target = reco_detail.best_result.box
            if self._check_lock(context, image, target):
                logger.info(f"分数 {target_threshold:,} 已锁定，降档重试")
                option_idx -= 1
                continue

            x = target[0] + target[2] // 2
            y = target[1] + target[3] // 2
            break

        logger.info(f"当前镜像: {current_mirror}, 当前投票: {vote}, 目标分数: {target_threshold:,}, 点击坐标: ({x}, {y - 20})")
        self._double_click(context, x, y - 20)

        return True

    @staticmethod
    def _check_lock(context: Context, image, target_box) -> bool:
        """检查目标分数附近是否有锁定图标"""
        roi = [
            target_box[0] + 330,
            target_box[1] - 80,
            100,
            100,
        ]
        reco_detail = context.run_recognition(
            "ProduceRecognitionLock",
            image,
            pipeline_override={
                "ProduceRecognitionLock": {
                    "recognition": "TemplateMatch",
                    "roi": roi,
                    "template": "produce/lock.png",
                    "green_mask": True,
                }
            },
        )
        return reco_detail is not None and reco_detail.hit

    def _get_current_mirror(self, context: Context, image) -> Optional[tuple]:
        """获取当前镜像"""
        mirror_list = {
            "first": "produce/NIA/mirror_1.png",
            "second": "produce/NIA/mirror_2.png",
            "third": "produce/NIA/mirror_3.png",
        }
        mirror_name = None
        for mirror_name, mirror_img in mirror_list.items():
            reco_detail = context.run_recognition(
                "ProduceMirrorFlag",
                image,
                pipeline_override={"ProduceMirrorFlag": {"template": mirror_img, "threshold": 0.9}},
            )
            if reco_detail and reco_detail.hit:
                return mirror_name, reco_detail.best_result.box

    @staticmethod
    def _get_current_vote(context: Context, image) -> Optional[int]:
        """获取当前投票"""
        reco_detail = context.run_recognition("ProduceRecognitionVote", image)
        if reco_detail and reco_detail.hit:
            try:
                vote = int(reco_detail.best_result.text.replace(",", ""))
                return vote
            except ValueError:
                logger.warning("投票数据解析失败")
                return None

    def _double_click(self, context: Context, x: int, y: int):
        """执行双击操作"""
        context.tasker.controller.post_click(x, y).wait()
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(x, y).wait()


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
                context.tasker.controller.post_click(box[0] + int(box[2] / 2), box[1] + int(box[3] / 2)).wait()
                time.sleep(0.1)
            context.run_task("ProduceDrinkNoButton")
        return True
