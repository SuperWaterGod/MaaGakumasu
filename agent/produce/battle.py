"""培育出牌动作；流水线注册名称与原行为保持不变。"""

import time
from collections import Counter

from utils import logger
from maa.context import Context
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer

from .capture import DecisionCapture
from .console import log_operation
from .sessions import SESSIONS


def card_candidates(results, y_min=840, y_max=1000):
    """检测置信度只表示识别可靠程度；被过滤的框仍保留为证据。"""
    return [
        {
            "candidate_id": f"detection_{index}",
            "box": [int(value) for value in result.box],
            "label": result.label,
            "detector_confidence": float(result.score),
            "in_hand_region": y_min <= result.box[1] <= y_max,
            "available": result.label in ("cards", "suggestions") and y_min <= result.box[1] <= y_max,
            "card_id": None,
        }
        for index, result in enumerate(results)
    ]


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

    # 卡牌Y轴位置边界（超出范围视为识别异常，不点击）
    CARD_Y_MIN = 840
    CARD_Y_MAX = 1000

    def __init__(self):
        super().__init__()
        self.start_time = time.time()
        self._capture = None
        self._context = None
        self._active_decision = None
        self._pending_after = None
        self._battle_id = None
        self._last_wait_image = None
        self._frame_observed_at_ns = None

    def run(
        self,
        context: Context,
        argv: CustomAction.RunArg,
    ) -> bool:
        self._context = context
        session = SESSIONS.get(context)
        self._capture = session["capture"] if session else DecisionCapture.from_config({
            "mode": "agent_actions", "action": "ProduceCardsAuto", "entry": getattr(argv, "node_name", None),
            "coverage": "single_action_invocation", "policy": "existing_auto", "game_action_confirmation": "unknown",
        }, console=logger.info, context=context)
        self._battle_id = self._record("battle_invocation_start", node=getattr(argv, "node_name", None))
        reason = "action_returned"
        try:
            return self._run(context, argv)
        except Exception:
            reason = "action_exception"
            raise
        finally:
            self._flush_decision()
            self._record("battle_invocation_end", reason=reason, task_stopping=context.tasker.stopping)
            if self._capture and not session:
                self._capture.close("task_stopping" if context.tasker.stopping else reason)
            self._capture = None
            self._context = None
            self._active_decision = None
            self._battle_id = None

    def _record(self, kind, image=None, **data):
        if self._capture:
            return self._capture.record(kind, image=image, context=self._context, battle_id=self._battle_id, **data)
        return None

    def _begin_decision(self, kind, image, results=(), **data):
        if not self._capture:
            return
        self._active_decision = None
        try:
            self._active_decision = self._record(
                "decision_before", image, operation=kind, candidates=card_candidates(results),
                source="existing_auto", frame_observed_at_ns=self._frame_observed_at_ns, **data,
            )
            self._flush_decision(self._active_decision)
        except Exception as exc:
            logger.warning(f"采集候选失败，继续原任务: {exc}")

    def _finish_decision(self, wait_result, *, selection_click_seconds=None):
        self._pending_after = {
            "decision_id": self._active_decision, "wait_until_playable": wait_result,
            "game_action_confirmed": None, "selection_click_seconds": selection_click_seconds,
            "observation_phase": "next_decision_before",
        }
        self._active_decision = None

    def _flush_decision(self, frame_reference=None):
        if self._pending_after:
            self._record(
                "decision_after", frame_reference=frame_reference, **self._pending_after,
            )
            self._pending_after = None

    def _log_battle_exit(self, context):
        self._flush_decision()
        logger.info("战斗－退出出牌（未检测到体力标志）")

    def _run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        # 使用饮料
        self._wait_until_playable(context)
        image = context.tasker.controller.post_screencap().wait().get()
        self._frame_observed_at_ns = time.time_ns()
        reco_detail = context.run_recognition("ProduceCheckDrinkButton", image)
        hit_count = len(reco_detail.filtered_results)  # 获取饮料数
        self._record("battle_entry", drink_detections=hit_count)
        logger.info(f"识别到{hit_count}瓶饮料")
        for _ in range(hit_count):
            self._begin_decision("use_drink_task", image, reason="existing_use_all_drinks")
            context.run_task("ProduceUseDrink")
            playable = self._wait_until_playable(context, 2)
            self._finish_decision(playable)
            image = self._last_wait_image

        # 开始出牌
        self._reset_window()
        while True:
            # 处理手动终止任务
            if context.tasker.stopping:
                logger.info("任务中断")
                return True

            # 截图
            image = context.tasker.controller.post_screencap().wait().get()
            self._frame_observed_at_ns = time.time_ns()

            # 通过检测体力槽判断是否处于出牌场景
            if not self._is_playing_card(context, image):
                # 动画可能暂时遮住体力标志，复用等待逻辑复核，恢复后重新截图。
                if self._wait_until_playable(context):
                    continue
                if context.tasker.stopping:
                    return True
                self._record("battle_exit_observation", reason="health_flag_not_detected")
                self._log_battle_exit(context)
                break

            # 识别手牌
            reco_detail = context.run_recognition("ProduceRecognitionCards", image)
            if reco_detail and reco_detail.hit:
                # 获取全部检测结果，再按手牌区域过滤候选
                results = reco_detail.all_results

                # 获取卡牌信息
                suggestions, useless, cards, suggestions_box, best_box = self._get_card_info(results)
                # print(f"卡片数量:{suggestions}/{cards}/{useless}")

                # 有推荐牌时，打出推荐牌
                if suggestions > 0:
                    if suggestions_box[1] < self.CARD_Y_MIN or suggestions_box[1] > self.CARD_Y_MAX:
                        time.sleep(self.CLICK_DELAY)
                        continue
                    self._play_a_card(context, suggestions_box, image, results, "game_recommendation")
                # 只有一张可用牌时，直接打出该牌
                elif cards == 1:
                    if best_box[1] < self.CARD_Y_MIN or best_box[1] > self.CARD_Y_MAX:
                        time.sleep(self.CLICK_DELAY)
                        continue
                    time.sleep(1)  # 防止点击过早导致只命中一次
                    self._play_a_card(context, best_box, image, results, "only_available_card")
                # 没有可用牌时，先判断是否处于出牌场景，确认处于出牌场景后，再跳过回合
                elif useless > 0 and suggestions == 0 and cards == 0:
                    logger.warning("战斗 - 无可用牌，跳过回合")
                    self._begin_decision("skip_round", image, results, reason="no_available_cards")
                    context.run_task("ProduceRecognitionSkipRound")
                    playable = self._wait_until_playable(context)
                    self._finish_decision(playable)
                    self._reset_window()

                end_time = time.time()
                if end_time - self.start_time > self.TIME_OUT:
                    if best_box[1] < self.CARD_Y_MIN or best_box[1] > self.CARD_Y_MAX:
                        time.sleep(self.CLICK_DELAY)
                        continue

                    logger.warning("检测超时")
                    self._play_a_card(context, best_box, image, results, "timeout_detector_confidence")

            else:
                reco_detail = context.run_recognition("ProduceRecognitionNoCards", image)
                if reco_detail and reco_detail.hit:
                    if not (self._capture and self._capture.merged_console_enabled):
                        logger.info("战斗 - 无手牌，跳过回合")
                    self._begin_decision("skip_round", image, reason="no_cards_ocr")
                    context.run_task("ProduceRecognitionSkipRound")
                    playable = self._wait_until_playable(context)
                    self._finish_decision(playable)
                    self._reset_window()

            time.sleep(self.CLICK_DELAY)
        return True

    def _reset_window(self):
        """重置出牌计时窗口"""
        self.start_time = time.time()

    @classmethod
    def _get_card_info(cls, results: list):
        """
        从手牌区域内的识别结果中获取卡牌信息，忽略场景中的误检

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
        suggestions_score = 0
        for result in results:
            # 先过滤再计数/选牌。越界推荐牌不能阻止有效普通牌的超时兜底，
            # 越界高分普通牌也不能覆盖手牌区域内的候选。
            if not cls.CARD_Y_MIN <= result.box[1] <= cls.CARD_Y_MAX:
                continue
            label_counts[result.label] += 1
            if result.label == "suggestions" and result.score > suggestions_score:
                suggestions_score = result.score
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

    def _play_a_card(self, context: Context, box: list, image=None, results=(), reason="unspecified") -> bool:
        """
        出牌并处理移动卡牌界面

        Args:
            context: maa的Context类
            box: 点击范围，格式为[x, y, w, h]（x、y为点击范围左上角的坐标）

        Returns:
            bool: 如果执行没有问题，返回True；否则返回False。
        """

        self._begin_decision("play_card", image, results, selected_box=list(box), reason=reason)
        # 出牌
        # 调试示例：按检测框偏移位置点击卡牌。
        context.tasker.controller.post_click(box[0] + box[2] // 2, box[1] + box[3] // 2).wait()
        self._record("input_issued", decision_id=self._active_decision, click_index=1,
                     point=[box[0] + box[2] // 2, box[1] + box[3] // 2])
        time.sleep(self.CLICK_DELAY)
        context.tasker.controller.post_click(box[0] + box[2] // 2, box[1] + box[3] // 2).wait()
        self._record("input_issued", decision_id=self._active_decision, click_index=2,
                     point=[box[0] + box[2] // 2, box[1] + box[3] // 2])
        elapsed = time.time() - self.start_time
        logger.debug(f"出牌选择窗口至第二次点击：{elapsed:.2f} 秒")
        if not (self._capture and self._capture.merged_console_enabled):
            log_operation("战斗", "已发送出牌点击", {
                "选择依据": {"game_recommendation": "游戏推荐", "only_available_card": "仅一张可用",
                             "timeout_detector_confidence": "15 秒超时兜底"}.get(reason, reason),
                "选择等待及点击耗时": f"{elapsed:.2f} 秒（不含后续动画等待）",
                "actual_point": [box[0] + box[2] // 2, box[1] + box[3] // 2],
                "牌名": "牌名识别不可用，未知",
            })

        # 等待回到可出牌状态后，重置计时
        time.sleep(1)
        playable = self._wait_until_playable(context)
        self._finish_decision(playable, selection_click_seconds=elapsed)
        self._reset_window()

        return True

    def _handle_move_cards(self, context: Context, image=None) -> bool:
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
            self._record("move_cards_screen", image=image, decision_id=self._active_decision, operation="move_cards")
            y = 450
            while y < 1100:
                # 调试示例：按四个固定卡槽位置依次处理饮料。
                for x in [140, 285]:
                    context.tasker.controller.post_click(x, y).wait()
                    self._record("move_cards_input", decision_id=self._active_decision, point=[x, y], click_index=1)
                    time.sleep(0.2)
                    context.tasker.controller.post_click(x, y).wait()
                    self._record("move_cards_input", decision_id=self._active_decision, point=[x, y], click_index=2)
                    time.sleep(0.2)
                image = context.tasker.controller.post_screencap().wait().get()
                reco_detail = context.run_recognition("ProduceRecognitionChooseMoveCards", image)
                if not reco_detail.hit:
                    self._record("move_cards_selection_observation", decision_id=self._active_decision)
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
        # 调试示例：识别移动卡牌页面。
        # 检查识别是否命中。
        # 命中时返回成功。
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
            self._last_wait_image = image
            self._frame_observed_at_ns = time.time_ns()

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
            else:
                count_exit = 0

            # 解决莫名其妙的误触问题
            reco_detail = context.run_recognition("ProduceButton", image)
            if reco_detail and reco_detail.hit:
                self._record("confirmation_task", decision_id=self._active_decision, task="ProduceButton")
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
