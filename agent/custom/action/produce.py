"""MaaFramework 培育动作入口及奖励领取适配。"""

import json
import time

from utils import logger
from maa.context import Context
from produce.icons import load_gallery, icon_operation_available
from produce.battle import ProduceCardsAuto
from produce.events import ProduceChooseEventAuto, ProduceChooseEventBase, ProduceChooseNIAEventAuto
from produce.capture import ROOT, produce_fast_mode
from produce.console import reward_title
from produce.options import ProduceKeepDrinkAuto, ProduceChooseWorkAuto, ProduceChooseMirrorAuto, ProduceChooseOptionsAuto
from produce.rewards import observe_reward_page
from produce.sessions import SESSIONS
from maa.custom_action import CustomAction
from produce.resources import RecoveryError, UpdateCancelled, configured_updater, icon_resources_status, ensure_resources_ready
from produce.selection import debug_choice
from maa.agent.agent_server import AgentServer

__all__ = [
    "ProduceUpdateResources", "ProduceResourcesReady",
    "ProduceChooseEventBase", "ProduceChooseEventAuto", "ProduceChooseNIAEventAuto", "ProduceCardsAuto",
    "ProduceChooseWorkAuto", "ProduceChooseOptionsAuto", "ProduceChooseMirrorAuto", "ProduceKeepDrinkAuto",
    "ProduceRewardObserve", "ProduceRewardConfirm", "ProduceSkipShowAuto",
    "ProduceEditChoice", "ProduceSettlementClick",
]


@AgentServer.custom_action("ProduceEditChoice")
class ProduceEditChoice(CustomAction):
    """沿用原页面两次强化点击及确认流程，只替换选牌位置。"""

    def run(self, context, argv):
        operation = json.loads(argv.custom_action_param)["operation"]
        session = SESSIONS.get(context)
        default = [140, 760] if operation.startswith("strengthen") else [140, 450]
        point = default
        if operation == "strengthen_repeat":
            point = session.pop("strengthen_point", default) if session else default
        else:
            capture = session["capture"] if session else None
            choice = None
            if capture and capture.selection and capture.reward_reader.icons:
                image = context.tasker.controller.post_screencap().wait().get()
                candidates = capture.reward_reader.icons.scan(image, operation, allow_unverified_variant=True)
                policy = capture.selection
                policy.observe(c["catalog_id"] for c in candidates)
                if operation == "delete_copy_exchange":
                    for kind in ("delete", "copy", "exchange"):
                        header = context.run_recognition("ProduceChooseDeleteFlag", image, pipeline_override={
                            "ProduceChooseDeleteFlag": {"recognition": {"param": {"template": [f"produce/{kind}_icon.png"]}}},
                        })
                        if header and header.hit:
                            operation = kind
                            break
                if operation in {"strengthen", "delete", "copy", "exchange"}:
                    choice = policy.choose(candidates, operation)
                debug_choice(capture, context, image, operation, choice)
                if choice:
                    x, y, w, h = choice["box"]
                    point = [x + w // 2, y + h // 2]
                # 调整中的卡暂时撤下；其余已确认持有的卡继续用于下一次判断。
                if operation != "copy":
                    target = choice or next((c for c in candidates if c["box"][0] <= point[0] < c["box"][0] + c["box"][2]
                                             and c["box"][1] <= point[1] < c["box"][1] + c["box"][3]), None)
                    target = target or {}
                    pending = session.get("shop_pending")
                    if operation == "strengthen" and pending and pending["operation"] == "strengthen":
                        pending["upgraded_id"] = policy.upgrades.get(target.get("catalog_id"))
                    policy.forget_card(target.get("catalog_id"), target.get("base_name"))
            label = {"strengthen": "强化", "delete": "删除", "exchange": "变换", "copy": "复制"}.get(operation, "卡牌调整")
            logger.info(f"{label}选择－自动 · {choice['database_name']}，{choice['reason']}" if choice else f"{label}选择－默认 · 第一项")
            if session and operation == "strengthen":
                session["strengthen_point"] = point
        return context.tasker.controller.post_click(*point).wait().succeeded


@AgentServer.custom_action("ProduceSettlementClick")
class ProduceSettlementClick(CustomAction):
    """结算等待可能跨页，只点击当前画面仍可见的按钮。"""

    def run(self, context, argv):
        if context.tasker.stopping:
            return False
        controller = context.tasker.controller
        image = controller.post_screencap().wait().get()
        detail = context.run_recognition(argv.node_name, image)
        if not detail or not detail.hit:
            return True
        x, y, width, height = detail.box
        clicked = controller.post_click(x + width // 2, y + height // 2).wait().succeeded
        if clicked and argv.node_name == "ProduceGeneration":
            logger.info("结算 · 已点生成")
        return clicked


@AgentServer.custom_action("ProduceResourcesReady")
class ProduceResourcesReady(CustomAction):
    def run(self, context, argv):
        if produce_fast_mode(context):
            logger.info("快速模式：跳过培育采集、额外识别与报告")
            return not context.tasker.stopping
        try:
            ensure_resources_ready(ROOT)
            icon_resources_status.cache_clear()
            load_gallery.cache_clear()
            status = icon_resources_status(ROOT)
            labels = {"skill_card": "技能卡", "drink": "饮料", "p_item": "P 道具"}
            missing = [f"{labels[kind]}未安装" if not row["expected"] else
                       f"{labels[kind]}缺失 {row['missing']} 张、校验失败 {row['damaged']} 张"
                       for kind, row in status.items() if not row["complete"]]
            if missing:
                operations = {"shop": "商店", "strengthen": "强化", "delete_copy_exchange": "删除／复制／变换", "move_cards": "移动选牌"}
                degraded = [name for operation, name in operations.items() if not icon_operation_available(status, operation)]
                logger.warning("完整模式图库未就绪（{}）。{}的图标专用环节按快速模式执行，继续 OCR、记录和回放。"
                               "可通过“一键培育”的“更新数据库和素材”任务补全图库。",
                               "，".join(missing), "、".join(degraded))
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
            updater.update()
            load_gallery.cache_clear()
            icon_resources_status.cache_clear()
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
    """整页匹配奖励图标，补读未知候选后交给流水线选择。"""

    def run(self, context, argv):
        if context.tasker.stopping:
            return False
        if produce_fast_mode(context):
            return True
        session = SESSIONS.get(context)
        if not session:
            return True
        session.pop("reward_page", None)
        session.pop("reward_choice", None)
        session.pop("rule_reward", None)
        capture = session["capture"]
        if not capture.reward_preview_enabled and not capture.selection:
            return True
        try:
            image = context.tasker.controller.post_screencap().wait().get()
            page = observe_reward_page(capture, context, image, session.get("reward_kind"))
            session["reward_page"] = page
            previous = session.pop("await_reward_transition", None)
            if previous and page and capture.selection:
                ids = [c["catalog_id"] for c in page["candidates"]]
                old = previous["candidates"]
                if len(ids) != len(old) or any(a and b and a != b for a, b in zip(ids, old)):
                    capture.selection.acquired(previous["selected"])
        except Exception:
            logger.exception("奖励候选识别失败，继续按流水线优先级选择")
        return not context.tasker.stopping


@AgentServer.custom_action("ProduceRewardConfirm")
class ProduceRewardConfirm(CustomAction):
    def run(self, context, argv):
        if context.tasker.stopping:
            return False
        task_id = context.get_task_job().job_id
        session = SESSIONS.get(context) if not produce_fast_mode(context) else None
        choice = (session or {}).get("reward_choice") or SESSIONS.reward_choices.get(task_id)
        action = context.run_action("ProduceRewardReceive", box=argv.box)
        success = bool(action and action.success)
        if success:
            SESSIONS.reward_choices.pop(task_id, None)
        if session:
            session["reward_receive_action"] = action
            if self.record_receive(context, session, argv.box, action, success, choice):
                return success
        self.log_receive(success, choice)
        return success

    @staticmethod
    def record_receive(context, session, box, action, success, choice):
        page = session.pop("reward_page", None)
        if not page:
            return False
        candidate = None
        point = (choice or {}).get("actual_point")
        if point and choice.get("policy_node") != "ProduceChooseSkip":
            px, py = point
            for item in page["candidates"]:
                x, y, width, height = item["box"]
                if x <= px < x + width and y <= py < y + height:
                    candidate = item
                    break
        if success and candidate and candidate["catalog_id"]:
            session["await_reward_transition"] = {
                "selected": candidate["catalog_id"], "candidates": [c["catalog_id"] for c in page["candidates"]],
            }
        capture = session["capture"]
        if not page["group_id"]:
            return False
        event_id = capture.record(
            "reward_receive_input", context=context, reward_page_id=page["group_id"], reward_kind=session.get("reward_kind"),
            confirmation_id=session.get("reward_confirmation_id"), policy_choice=choice,
            selected_candidate_id=candidate["candidate_id"] if candidate else None,
            actual_box=list(box), framework_action_succeeded=success, game_action_confirmed=None,
            actual_point=list(action.result.point) if action and getattr(action.result, "point", None) else None,
        )
        return bool(event_id and capture.console_emitted(event_id))

    @staticmethod
    def log_receive(success, choice):
        (logger.info if success else logger.warning)(reward_title(success, choice))


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

