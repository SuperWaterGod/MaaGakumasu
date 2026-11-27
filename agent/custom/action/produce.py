"""MaaFramework 培育动作入口及奖励领取适配。"""

import json

from utils import logger
from maa.context import Context
from produce.icons import load_gallery, icon_operation_available
from produce.battle import ProduceCardsAuto
from produce.events import ProduceChooseEventAuto, ProduceChooseEventBase, ProduceChooseNIAEventAuto
from produce.capture import ROOT, produce_fast_mode, produce_battle_rules_enabled
from produce.console import reward_title
from produce.options import ProduceKeepDrinkAuto, ProduceChooseWorkAuto, ProduceChooseMirrorAuto, ProduceChooseOptionsAuto
from produce.rewards import observe_reward_page
from produce.sessions import SESSIONS
from maa.custom_action import CustomAction
from produce.resources import (
    BATTLE_RULES_NAME,
    RecoveryError,
    UpdateCancelled,
    data_directory,
    configured_updater,
    ensure_resources_ready,
    produce_resources_status,
)
from produce.selection import rules, debug_choice
from utils.task_control import wait_running, click_running
from produce.battle_policy import load_battle_rules
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
    """选择调整对象，并为强化补点复用原选牌坐标。"""

    def run(self, context, argv):
        if context.tasker.stopping:
            return False
        operation = json.loads(argv.custom_action_param)["operation"]
        session = SESSIONS.get(context)
        default = [140, 760] if operation.startswith("strengthen") else [140, 450]
        point = default
        target = None
        if operation == "strengthen_repeat":
            point = session.pop("strengthen_point", default) if session else default
        else:
            capture = session["capture"] if session else None
            choice = None
            pending = session.get("shop_pending") if session else None
            if capture and capture.selection and capture.reward_reader.icons:
                image = context.tasker.controller.post_screencap().wait().get()
                candidates = capture.reward_reader.icons.scan(image, operation, allow_unverified_variant=True)
                policy = capture.selection
                policy.observe_card_list((c.get("catalog_id") for c in candidates),
                                         (c.get("base_name") for c in candidates if not c.get("catalog_id")))
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
                target = choice or next((c for c in candidates if c["box"][0] <= point[0] < c["box"][0] + c["box"][2]
                                         and c["box"][1] <= point[1] < c["box"][1] + c["box"][3]), {})
            if pending and pending["operation"] == "delete" and operation in {"delete", "delete_copy_exchange"} and not choice:
                image = context.tasker.controller.post_screencap().wait().get()
                cancel = context.run_recognition("ProduceShoppingCancelEdit", image)
                if not cancel or not cancel.hit:
                    return False
                x, y, w, h = cancel.box
                if not click_running(context.tasker, x + w // 2, y + h // 2):
                    return False
                session.pop("shop_pending", None)
                logger.info("商店 · 删除列表没有可核实的目标，已点取消")
                return True
            label = {"strengthen": "强化", "delete": "删除", "exchange": "变换", "copy": "复制"}.get(operation, "卡牌调整")
            if choice:
                name = choice.get("display_name") or choice["database_name"]
                logger.info(f"{label}选择－自动 · {name}，{choice['reason']}")
            else:
                logger.info(f"{label}选择－默认 · 第一项")
            if session and operation == "strengthen":
                session["strengthen_point"] = point
        clicked = click_running(context.tasker, *point)
        if clicked and operation != "strengthen_repeat":
            pending = session.get("shop_pending") if session else None
            shop_operation = "delete" if operation == "delete_copy_exchange" else operation
            if pending and pending["operation"] == shop_operation:
                # 商店选牌和付费是两步；先保存目标，回到货架核实扣款后再更新持牌。
                target = target or {}
                pending["edit_target"] = {key: target.get(key) for key in ("catalog_id", "base_name")}
            elif target is not None:
                session["pending_card_edit"] = (operation, target.get("catalog_id"), target.get("base_name"))
        return clicked


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
        clicked = click_running(context.tasker, x + width // 2, y + height // 2)
        if clicked and argv.node_name == "ProduceGeneration":
            logger.info("结算 · 已点生成")
        return clicked


@AgentServer.custom_action("ProduceResourcesReady")
class ProduceResourcesReady(CustomAction):
    def run(self, context, argv):
        if produce_fast_mode(context):
            logger.info("精简模式－采用游戏推荐，跳过培育采集、额外识别与报告")
            return not context.tasker.stopping
        produce_resources_status.cache_clear()
        load_gallery.cache_clear()
        status, database_error = produce_resources_status(ROOT)
        warning = None
        if database_error is not None:
            logger.warning("培育数据库校验未通过：{}", database_error)
            warning = ("培育数据库不可用。将继续自动培育、OCR、记录和回放。"
                       "奖励选择、商店、强化及其他图标辅助选牌将使用默认流程。")
        else:
            try:
                rules()
            except (OSError, ValueError) as exc:
                logger.error(f"完整模式奖励规则无法加载：{exc}")
                return False
            labels = {"skill_card": "技能卡", "drink": "饮料", "p_item": "P 道具"}
            missing = [f"{labels[kind]}未安装" if not row["expected"] else
                       f"{labels[kind]}缺失 {row['missing']} 张、校验失败 {row['damaged']} 张"
                       for kind, row in status.items() if not row["complete"]]
            if missing:
                operations = {"shop": "商店", "strengthen": "强化", "delete_copy_exchange": "删除／复制／变换", "move_cards": "移动选牌"}
                degraded = [name for operation, name in operations.items() if not icon_operation_available(status, operation)]
                warning = (f"培育图库未就绪（{'，'.join(missing)}）。"
                           f"{'、'.join(degraded)}的图标辅助环节将使用默认流程，继续 OCR、记录和回放。")
        warnings = [warning + "可通过「一键培育」中的「更新数据库和素材」补全资源。"] if warning else []
        if produce_battle_rules_enabled(context):
            try:
                load_battle_rules(data_directory(ROOT) / BATTLE_RULES_NAME)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                logger.warning(f"完整模式战斗规则无法加载：{exc}")
                warnings.append(f"战斗规则缺失或无效，本次培育沿用游戏推荐、单张和超时兜底，保留饮料。请从同版本客户端补全 data/{BATTLE_RULES_NAME} 后重新开始任务。")
        if warnings and not context.tasker.stopping:
            message = "\n".join(warnings)
            logger.warning(message)
            node = "ProduceResourceWarning"
            context.run_action(node, pipeline_override={node: {"focus": {
                "Node.ActionNode.Succeeded": {
                    "content": f"[color:orange]培育资源未就绪[/color]\n{message}",
                    "display": ["modal"],
                }
            }}})
        SESSIONS.prepare_reports()
        return not context.tasker.stopping


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
            produce_resources_status.cache_clear()
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
                ids = [c.get("catalog_id") for c in page["candidates"]]
                old = previous["candidates"]
                if len(ids) != len(old) or any(a and b and a != b for a, b in zip(ids, old)):
                    capture.selection.acquired(previous["selected"], previous.get("base_name"))
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
        if session and choice and choice.get("policy_node") != "ProduceChooseByRule":
            page = session.get("reward_page")
            reason = (page or {}).get("fallback_reason")
            if reason:
                choice = {**choice, "fallback_reason": reason}
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
        if success and candidate and (candidate.get("catalog_id") or candidate.get("base_name")):
            session["await_reward_transition"] = {
                "selected": candidate.get("catalog_id"), "base_name": candidate.get("base_name"),
                "candidates": [c.get("catalog_id") for c in page["candidates"]],
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
            if context.tasker.stopping:
                return False
            image = context.tasker.controller.post_screencap().wait().get()
            if image is None or image.size == 0:
                return False
            height, width = image.shape[:2]
            if height >= width:
                # 演出已结束或尚在切屏，交还流水线判断，避免点击结算页面。
                return True

            x = max(0, min(width - 1, target_x))
            y = max(0, min(height - 1, target_y))
            if not click_running(context.tasker, x, y) or not wait_running(context.tasker, .3):
                return False

        return True

