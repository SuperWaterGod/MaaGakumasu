"""MaaFramework 培育动作入口及奖励领取适配。"""

import time

from utils import logger
from maa.context import Context
from produce.battle import ProduceCardsAuto
from produce.events import ProduceChooseEventAuto, ProduceChooseEventBase, ProduceChooseNIAEventAuto
from produce.capture import ROOT, produce_fast_mode
from produce.console import policy_name
from produce.options import ProduceKeepDrinkAuto, ProduceChooseWorkAuto, ProduceChooseMirrorAuto, ProduceChooseOptionsAuto
from produce.rewards import RewardPreview, verify_reward_layout
from produce.sessions import SESSIONS
from maa.custom_action import CustomAction
from produce.resources import RecoveryError, UpdateCancelled, configured_updater, ensure_resources_ready
from maa.agent.agent_server import AgentServer

__all__ = [
    "ProduceUpdateResources", "ProduceResourcesReady",
    "ProduceChooseEventBase", "ProduceChooseEventAuto", "ProduceChooseNIAEventAuto", "ProduceCardsAuto",
    "ProduceChooseWorkAuto", "ProduceChooseOptionsAuto", "ProduceChooseMirrorAuto", "ProduceKeepDrinkAuto",
    "ProduceRewardObserve", "ProduceRewardConfirm", "ProduceSkipShowAuto",
]


@AgentServer.custom_action("ProduceResourcesReady")
class ProduceResourcesReady(CustomAction):
    def run(self, context, argv):
        if produce_fast_mode(context):
            logger.info("快速模式：跳过培育采集、额外识别与报告")
            return not context.tasker.stopping
        try:
            ensure_resources_ready(ROOT)
            SESSIONS.prepare_reports()
            return not context.tasker.stopping
        except Exception as exc:
            logger.error(f"培育资源不可用，请先执行“更新数据库和素材”：{exc}")
            return False


@AgentServer.custom_action("ProduceUpdateResources")
class ProduceUpdateResources(CustomAction):
    def run(self, context, argv):
        if SESSIONS.active:
            logger.warning("存在活动培育会话，跳过数据库和素材更新")
            return True
        try:
            updater = configured_updater(ROOT, stopping=lambda: context.tasker.stopping, log=logger.info, debug=logger.debug)
            result = updater.update()
            if result["status"] == "updated":
                from produce.icons import load_gallery

                load_gallery.cache_clear()
            return True
        except UpdateCancelled:
            logger.info("资源更新已取消，下载断点保留在 temp/produce_update")
            return False
        except Exception as exc:
            logger.warning(f"数据库和素材更新未完成：{exc}")
            try:
                ensure_resources_ready(ROOT)
            except (RecoveryError, OSError, ValueError) as recovery:
                logger.error(f"资源需修复，停止后续培育：{recovery}")
                return False
            logger.warning("继续使用现有数据库和素材；缺失图标保留未知")
            return not context.tasker.stopping


@AgentServer.custom_action("ProduceRewardObserve")
class ProduceRewardObserve(CustomAction):
    """完整模式先识别奖励候选，再交给流水线执行配置的选择优先级。"""

    def run(self, context, argv):
        if context.tasker.stopping:
            return False
        if produce_fast_mode(context):
            return True
        session = SESSIONS.get(context)
        if not session:
            return True
        session.pop("reward_preview", None)
        session.pop("reward_choice", None)
        capture = session["capture"]
        if not capture.reward_preview_enabled:
            return True
        preview = None
        try:
            preview = self.preview(context, session)
            image = preview.screenshot()
            skip = context.run_recognition("ProduceChooseSkip", image)
            if skip and skip.hit:
                return True
            result = preview.collect()
        except Exception:
            logger.exception("奖励候选采集未完成，交由流水线选择并核验")
            result = {"status": "skipped", "reason": "preview_error"}
        # 跨动作只保留画面和普通数据，不能复用已结束回调的 Context 或输入闭包。
        if preview and preview.group_id and preview.original is not None:
            if preview.changed_selection:
                try:
                    image = preview.screenshot()
                    layout = verify_reward_layout(image, preview.original, preview.layout)
                    ready = layout["status"] in {"ready", "selection_unconfirmed"} and preview.receive_visible(image)
                except Exception:
                    logger.exception("奖励预览后的页面核验失败")
                    ready = False
                if not ready:
                    logger.warning("奖励候选页面已变化或未确认，暂停选择")
                    context.tasker.post_stop()
                    return False
            session["reward_preview"] = {
                "group_id": preview.group_id, "layout": preview.layout,
                "original": preview.original, "result": result,
            }
        elif preview and preview.changed_selection:
            logger.warning("奖励候选页面未确认，暂停任务")
            context.tasker.post_stop()
            return False
        return not context.tasker.stopping

    @staticmethod
    def preview(context, session):
        def receive_visible(image):
            detail = context.run_recognition("ProduceRewardReceive", image)
            return bool(detail and detail.hit)

        def screenshot():
            image = context.tasker.controller.post_screencap().wait().get()
            return image.copy()

        def recognize(kind, params, image):
            detail = context.run_recognition_direct(kind, params, image)
            return detail.all_results if detail else []

        return RewardPreview(
            session["capture"], screenshot,
            lambda x, y: context.tasker.controller.post_click(x, y).wait().status.succeeded,
            receive_visible, lambda: context.tasker.stopping, recognize=recognize, context=context, item_kind=session.get("reward_kind"),
        )


@AgentServer.custom_action("ProduceRewardConfirm")
class ProduceRewardConfirm(CustomAction):
    def run(self, context, argv):
        if context.tasker.stopping:
            return False
        if produce_fast_mode(context):
            return self.receive(context, argv.box)
        session = SESSIONS.get(context)
        if not session:
            return self.receive(context, argv.box)
        session.pop("reward_receive_action", None)
        pending = session.get("reward_preview")
        choice = session.get("reward_choice") or {}
        if not pending or choice.get("policy_node") == "ProduceChooseSkip":
            return self.receive(context, argv.box, session)
        capture = session["capture"]
        preview = ProduceRewardObserve.preview(context, session)
        preview.group_id = pending["group_id"]
        preview.layout = pending["layout"]
        preview.original = pending["original"]
        try:
            image = preview.screenshot()
            layout = verify_reward_layout(image, preview.original, preview.layout)
            # 同时核对流水线实际输入位置，避免把点击未生效后的旧选中项当成策略选择。
            point = choice.get("actual_point")
            boxes = [candidate["box"] for candidate in preview.layout["candidates"]]
            selected = min(range(len(boxes)), key=lambda index: abs(boxes[index][0] + boxes[index][2] / 2 - point[0])) if point else None
            target = boxes[selected] if selected is not None else None
            # 原流水线的随机点击区域与图标外框有少量偏差，允许 16 像素边缘余量。
            on_target = bool(point and target and target[0] - 16 <= point[0] < target[0] + target[2] + 16
                             and target[1] - 16 <= point[1] < target[1] + target[3] + 16)
            frame = preview.wait_selected(selected) if layout["status"] in {"ready", "selection_unconfirmed"} and on_target else None
        except Exception:
            logger.exception("奖励最终选择核验失败")
            selected, frame = None, None
        preview.record("reward_selection_confirmed", frame, selected_candidate_id=f"reward_{selected + 1}" if selected is not None else None,
                       verified=frame is not None, policy_choice=choice, confirmation_id=session.get("reward_confirmation_id"),
                       game_action_confirmed=None)
        if frame is None or context.tasker.stopping:
            logger.warning("奖励最终选中项未确认，暂停领取")
            context.tasker.post_stop()
            return False
        detail = context.run_recognition("ProduceRewardReceive", frame)
        if not detail or not detail.hit:
            logger.warning("奖励领取按钮未确认，暂停任务")
            context.tasker.post_stop()
            return False
        box = detail.box
        success = self.receive(context, box, session, log_result=False)
        action = session.get("reward_receive_action")
        receive_event_id = preview.record(
            "reward_receive_input", confirmation_id=session.get("reward_confirmation_id"),
            selected_candidate_id=f"reward_{selected + 1}",
            actual_box=list(box), framework_action_succeeded=success, game_action_confirmed=None,
            actual_point=list(action.result.point) if action and getattr(action.result, "point", None) else None,
        )
        if receive_event_id and preview.group_id and capture.merged_console_enabled:
            emitted = capture.console_emitted(receive_event_id)
            if not emitted and not context.tasker.stopping:
                self.log_receive(success, choice)
        else:
            result = pending["result"]
            self.log_receive(success, choice, preview_failure=result.get("reason") if result.get("status") == "skipped" else None)
        return success

    @staticmethod
    def receive(context, box, session=None, log_result=True):
        if context.tasker.stopping:
            return False
        task_id = context.get_task_job().job_id
        choice = (session or {}).get("reward_choice") or SESSIONS.reward_choices.get(task_id)
        action = context.run_action("ProduceRewardReceive", box=box)
        if session:
            session["reward_receive_action"] = action
        success = bool(action and action.success)
        if log_result:
            ProduceRewardConfirm.log_receive(success, choice)
        if success:
            SESSIONS.reward_choices.pop(task_id, None)
            if session:
                session.pop("reward_preview", None)
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

