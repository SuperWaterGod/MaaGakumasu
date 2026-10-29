"""MaaFramework 培育动作入口及奖励领取适配。"""

import time

from utils import logger
from maa.context import Context
from produce.battle import ProduceCardsAuto
from produce.events import ProduceChooseEventAuto, ProduceChooseEventBase, ProduceChooseNIAEventAuto
from produce.console import policy_name
from produce.options import ProduceKeepDrinkAuto, ProduceChooseWorkAuto, ProduceChooseMirrorAuto, ProduceChooseOptionsAuto
from produce.rewards import RewardPreview
from produce.sessions import SESSIONS
from maa.custom_action import CustomAction
from maa.agent.agent_server import AgentServer

__all__ = [
    "ProduceChooseEventBase", "ProduceChooseEventAuto", "ProduceChooseNIAEventAuto", "ProduceCardsAuto",
    "ProduceChooseWorkAuto", "ProduceChooseOptionsAuto", "ProduceChooseMirrorAuto", "ProduceKeepDrinkAuto", "ProduceRewardConfirm", "ProduceSkipShowAuto",
]


@AgentServer.custom_action("ProduceRewardConfirm")
class ProduceRewardConfirm(CustomAction):
    def run(self, context, argv):
        if context.tasker.stopping:
            return False
        session = SESSIONS.get(context)
        if session:
            session.pop("reward_receive_action", None)
        capture = session["capture"] if session else None
        if not capture or not capture.reward_preview_enabled:
            return self.receive(context, argv.box, session)
        # 跳过无法领取的饮料属于原有策略动作，
        # 不作为候选预览，也不进入奖励选择流程。
        choice = session.get("reward_choice") or {}
        if choice.get("policy_node") == "ProduceChooseSkip":
            return self.receive(context, argv.box, session)

        def receive_visible(image):
            detail = context.run_recognition("ProduceRewardReceive", image)
            return bool(detail and detail.hit)

        def screenshot():
            image = context.tasker.controller.post_screencap().wait().get()
            return image.copy()

        def recognize(kind, params, image):
            detail = context.run_recognition_direct(kind, params, image)
            return detail.all_results if detail else []

        preview = RewardPreview(
            capture, screenshot,
            lambda x, y: context.tasker.controller.post_click(x, y).wait().status.succeeded,
            receive_visible, lambda: context.tasker.stopping, recognize=recognize, context=context, item_kind=session.get("reward_kind"),
        )
        try:
            result = preview.collect(choice, session.get("reward_confirmation_id"))
        except Exception:
            logger.exception("奖励详情采集未启动，继续原选择")
            # collect 会处理输入后的异常并尝试恢复原选择。
            # 超出该保护范围的异常不得确认已经变化的选项。
            result = {"safe_to_receive": not preview.changed_selection, "status": "skipped"}
        if context.tasker.stopping:
            return False
        if not result["safe_to_receive"]:
            logger.warning("奖励原选择未恢复或任务已停止，暂停领取；请检查当前选中项后继续任务")
            context.tasker.post_stop()
            return False
        if preview.last_image is not None:
            # 恢复原页面后重新识别领取按钮，
            # 不复用预览候选之前的识别结果。
            detail = context.run_recognition("ProduceRewardReceive", preview.last_image)
            if not detail or not detail.hit:
                logger.warning("奖励领取按钮未确认，暂停任务")
                context.tasker.post_stop()
                return False
            box = detail.box
        else:
            box = argv.box
        success = self.receive(context, box, session, log_result=False)
        action = session.get("reward_receive_action")
        receive_event_id = preview.record(
            "reward_receive_input", confirmation_id=session.get("reward_confirmation_id"),
            selected_candidate_id=(f"reward_{preview.layout['selected_index'] + 1}" if preview.group_id else None),
            actual_box=list(box), framework_action_succeeded=success, game_action_confirmed=None,
            actual_point=list(action.result.point) if action and getattr(action.result, "point", None) else None,
        )
        if receive_event_id and preview.group_id and capture.merged_console_enabled:
            emitted = capture.console_emitted(receive_event_id)
            if not emitted and not context.tasker.stopping:
                self.log_receive(success, choice)
        else:
            self.log_receive(success, choice, preview_failure=result.get("reason") if result.get("status") == "skipped" else None)
        return success

    @staticmethod
    def receive(context, box, session=None, log_result=True):
        if context.tasker.stopping:
            return False
        action = context.run_action("ProduceRewardReceive", box=box)
        if session:
            session["reward_receive_action"] = action
        success = bool(action and action.success)
        if log_result:
            ProduceRewardConfirm.log_receive(success, (session or {}).get("reward_choice"))
        return success

    @staticmethod
    def log_receive(success, choice, preview_failure=None):
        message = "已点击领取奖励" if success else "奖励领取按钮点击失败"
        if (choice or {}).get("policy_node") == "ProduceChooseSkip":
            message = "已点击跳过奖励确认" if success else "跳过奖励确认点击失败"
        message += f"｜{policy_name(choice)}"
        if preview_failure:
            from produce.console import format_operation
            reasons = {
                "ambiguous_spacing": "候选图标间距未确认",
                "ambiguous_frames": "候选图标边框未确认",
                "incomplete_frames": "候选图标未完整识别",
                "selection_unconfirmed": "当前选中项未确认",
                "unsupported_size": "截图尺寸不支持",
                "capture_unavailable": "记录不可用",
                "receive_button_missing": "领取按钮未确认",
            }
            message = format_operation("奖励选择 - " + message, [
                "候选名称：未识别",
                "详情缺失原因：" + reasons.get(preview_failure, "候选识别未完成"),
            ])
        (logger.info if success else logger.warning)(message)


@AgentServer.custom_action("ProduceSkipShowAuto")
class ProduceSkipShowAuto(CustomAction):
    """按固定坐标尝试右上角两次、左上角一次，并限制点击在当前画面内。"""

    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        for target_x, target_y in ((1192, 80), (1192, 80), (20, 20)):
            image = context.tasker.controller.post_screencap().wait().get()
            if image is None or image.size == 0:
                return False
            height, width = image.shape[:2]
            if height >= width:
                # 演出已结束或尚在切屏，交还流水线判断，避免点击结算页面。
                return True

            x = max(0, min(width - 1, target_x))
            y = max(0, min(height - 1, target_y))
            if not context.tasker.controller.post_click(x, y).wait().succeeded:
                return False
            time.sleep(0.3)

        return True

