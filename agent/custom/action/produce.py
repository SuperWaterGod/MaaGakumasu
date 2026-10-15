"""MaaFramework 培育动作入口及奖励领取适配。"""

from utils import logger
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
    "ProduceChooseWorkAuto", "ProduceChooseOptionsAuto", "ProduceChooseMirrorAuto", "ProduceKeepDrinkAuto", "ProduceRewardConfirm",
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

        preview = RewardPreview(
            capture, screenshot,
            lambda x, y: context.tasker.controller.post_click(x, y).wait().status.succeeded,
            receive_visible, lambda: context.tasker.stopping,
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
            emitted = capture.wait_for_console(receive_event_id, cancelled=lambda: context.tasker.stopping)
            if not emitted and not context.tasker.stopping:
                self.log_receive(success, choice, pending=True)
        else:
            self.log_receive(success, choice)
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
    def log_receive(success, choice, pending=False):
        message = "已点击领取奖励" if success else "奖励领取按钮点击失败"
        if (choice or {}).get("policy_node") == "ProduceChooseSkip":
            message = "已点击跳过奖励确认" if success else "跳过奖励确认点击失败"
        message += f"｜{policy_name(choice)}"
        if pending:
            message += "｜候选摘要仍在后台处理，完成后补发"
        (logger.info if success else logger.warning)(message)
