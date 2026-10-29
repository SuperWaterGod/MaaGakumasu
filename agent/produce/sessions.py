"""通过只读流水线监听，将路线和对局证据关联到整次培育会话。"""

import re
import json
import uuid
import atexit
import hashlib
from pathlib import Path
from datetime import datetime, timezone

from utils import logger
from maa.tasker import TaskerEventSink
from maa.context import ContextEventSink
from maa.event_sink import NotificationType

from .capture import ROOT, DecisionCapture

# 只观察流水线已有动作，不新增输入或识别。
ROUTE_NODES = {
    "ProduceChooseGetFlag": "reward",
    "ProduceChooseSkip": "reward_skip",
    "ProduceChooseRecommend": "reward",
    "ProduceChooseEventRecommend": "reward",
    "ProduceChooseFirst": "reward",
    "ProduceChooseConfirm": "reward_confirm",
    "ProduceShoppingFlag": "shop",
    "ProduceShoppingBuySale": "shop",
    "ProduceShoppingBuy": "shop_confirm",
    "ProduceShoppingDecide": "shop_confirm",
    "ProduceShoppingDrinkFull": "shop_unavailable",
    "ProduceShoppingExit": "shop_exit",
    "ProduceChooseStrengthenFlag": "strengthen",
    "ProduceChooseStrengthenClick": "strengthen",
    "ProduceChooseStrengthen": "strengthen_confirm",
    "ProduceChooseDeleteFlag": "delete_copy_exchange",
    "ProduceChooseDeleteClick": "delete_copy_exchange_confirm",
}
PREPARATION_NODES = {
    "ProduceChooseDifficulty",
    "ProduceChooseIdol",
    "ProduceChooseIdolNext",
    "ProduceChooseSupportNext",
    "ProduceChooseMemoryNext",
}
SETTLEMENT_NODES = {"ProduceGeneration", "ProduceFinished"}
SETTLEMENT_ENTRIES = SETTLEMENT_NODES | {"ProduceEnd"}
SETTLEMENT_NAVIGATION = {
    "ProduceNext", "ProduceEndNext", "ProduceButton", "ProduceNIAButton", "ProduceDecide", "DecideButton",
    "CloseRoundButton", "ProduceEventResultButton", "ProduceSkip", "ProduceSkipED",
    "ProduceSkipChatFlag", "ProduceYes", "ProduceSkipShowConfirm", "ProduceFollow", "Click_1",
}
PROCESS_NODES = {'ProduceShoppingFlag', 'ProduceGeneration', 'ProduceRecognitionWorkOptions', 'ProduceLackAPDecide', 'ProduceChooseIdolFirstDecide', 'ProduceNIAFailedRetryLastChallenge', 'ProduceExit', 'ProduceFailedStop', 'ProduceChooseDeleteFlag', 'ProduceChooseEventRecommend', 'ProduceWorkFlag', 'ProduceChooseGetFlag', 'ProduceFailedFlag', 'ProduceFailedRetryConfirm', 'ProduceNIAFailedFlag', 'ProduceLackAPCancel', 'ProduceChooseFirst', 'ProduceChooseStrengthenClick', 'ProduceSkipED', 'ProduceKeepDrinkFlag', 'ProduceNIAFailedRetryConfirm', 'ProduceHomeFlag', 'ProduceMirrorFlag', 'ProduceGuideFlag', 'ProduceLackAP', 'ProduceEnd', 'ProduceBackHome', 'ProduceChooseRecommend'}
ENTRY_NODES = {"ProduceStart", "ProduceContinue", "ProduceSkipPreparation", "ProduceEntryFlag"}
GAMEPLAY_NODES = {"ProduceEntry", "ProduceEntryNIA"}


def session_directory_name(bindings):
    """固定开局显示名称；身份未知或文件名非法时使用安全占位。"""
    idol = bindings.get("configured_idol") or {}
    def safe(value, fallback):
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or fallback)).strip(" .")[:48]
        return re.sub(r"\s+", "", value) or fallback
    difficulty = bindings.get("difficulty")
    if isinstance(difficulty, list):
        difficulty = "-".join(str(item) for item in difficulty)
    level = safe(bindings.get("scenario"), "未知剧本") + safe(difficulty, "未知难度")
    return (datetime.now().astimezone().strftime("%Y%m%d_%H%M_")
            + level + "_" + safe(idol.get("idol_name"), "未知偶像") + "_"
            + safe(idol.get("song_name"), "未知歌曲"))


def capture_window(session):
    return bool(session.get("settlement_window") or session.get("exam_result_capture_remaining", 0))


def save_json(path, value):
    from .reporting import atomic_json
    atomic_json(path, value)


def configured_bindings(context):
    def node(name):
        return context.get_node_data(name) or {}

    idol_node = node("ProduceChooseIdol")
    recognition = idol_node.get("recognition", {})
    idol = recognition.get("param", {}).get("custom_recognition_param", {})
    difficulty = node("ProduceChooseDifficulty").get("recognition", {}).get("param", {}).get("expected")
    entry = node("ProduceEntryFlag").get("next", [])
    entry_names = [item.get("name") if isinstance(item, dict) else item for item in entry]
    return {
        "configured_idol": idol,
        "idol_selection_enabled": idol_node.get("enabled", True),
        "difficulty": difficulty,
        "scenario": "NIA" if "ProduceEntryNIA" in entry_names else "初",
        "idol_level": None,
        "player_level": None,
        "deck": {"cards": None, "complete": False, "source": "visible_observations_only"},
        "node_settings": {
            name: node(name)
            for name in (
                "ProduceChooseIdol",
                "ProduceChooseDifficulty",
                "ProduceChooseScenario",
                "ProduceChooseEventFlag",
                "ProduceChooseNIAEventFlag",
                "ProduceSkipPreparation",
                "ProduceEntryFlag",
            )
        },
    }


class ProduceSessions:
    def __init__(self):
        self.active = {}
        self.disabled = set()
        self.task_origins = {}
        self.task_entry_times = {}

    @staticmethod
    def key(tasker):
        return hashlib.sha256(tasker.controller.uuid.encode()).hexdigest()[:24]

    def get(self, context):
        if not self.active:
            return None
        try:
            session = self.active.get(self.key(context.tasker))
            return session
        except Exception:
            logger.warning("无法关联培育会话，继续原任务")
            return None

    def ensure(self, context, entry="observed_mid_produce", started_at=None, start_source=None, *, fresh_start=False):
        key = self.key(context.tasker)
        if key in self.active:
            return self.active[key]
        if key in self.disabled:
            return None
        task_id = context.get_task_job().job_id
        bindings = configured_bindings(context)
        package_data = ROOT / "data" if (ROOT / "data").is_dir() else ROOT / "assets/data"
        resource = ROOT / "resource/base" if (ROOT / "resource/base").is_dir() else ROOT / "assets/resource/base"
        version_files = [
            package_data / "produce_skill_cards.json",
            package_data / "produce_reward_items.json",
            package_data / "idols_cards.json",
            resource / "model/detect/cards.onnx",
            *sorted(Path(__file__).parent.glob("*.py")),
            ROOT / "agent/utils/image_io.py",
            ROOT / "agent/custom/action/produce.py",
            *sorted((resource / "pipeline").glob("Produce*.json")),
            *sorted((resource / "image/produce/settlement").glob("*.png")),
        ]
        version_hashes = {
            (str(path.relative_to(ROOT)).replace("\\", "/") if path.is_relative_to(ROOT) else path.name): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in version_files
            if path.exists()
        }
        base = ROOT / "debug/produce_sessions"
        pointer = base / f"active_{key}.json"
        previous = None
        if pointer.exists() and not fresh_start:
            try:
                previous = json.loads(pointer.read_text(encoding="utf-8"))
                if not isinstance(previous, dict) or previous.get("schema_version") != 4:
                    previous = None
                elif not isinstance(previous.get("session_id"), str) or not re.fullmatch(r"[0-9a-f]{32}", previous["session_id"]):
                    previous = None
                elif (
                    not isinstance(previous.get("bindings"), dict)
                    or not isinstance(previous.get("start_observed"), bool)
                    or not isinstance(previous.get("settlement_observed"), bool)
                    or previous.get("status") not in ("recording", "interrupted")
                ):
                    previous = None
                elif not previous.get("gameplay_started_at"):
                    # 旧的仅准备阶段记录不属于已中断的游戏存档。
                    previous = None
            except (OSError, ValueError):
                logger.warning("培育续跑索引不可读，将创建部分会话")
            # 继续培育只证明进入了续跑路线，不能独立证明是同一游戏存档。
            # 配置不一致时分别记录，不能静默合并会话。
            if previous and previous.get("bindings", {}).get("configured_idol") != bindings["configured_idol"]:
                previous = None
            elif previous and any(previous.get("bindings", {}).get(field) != bindings[field] for field in ("scenario", "difficulty")):
                previous = None
        session_id = previous["session_id"] if previous else uuid.uuid4().hex
        bindings["deck"]["deck_id"] = f"{session_id}:deck"
        directory_name = previous["directory_name"] if previous else session_directory_name(bindings)
        if Path(directory_name).name != directory_name or directory_name in (".", ".."):
            directory_name = session_id
        session_directory = base / directory_name
        if not previous:
            base.mkdir(parents=True, exist_ok=True)
            number = 1
            while True:
                try:
                    session_directory.mkdir()
                    break
                except FileExistsError:
                    number += 1
                    session_directory = base / f"{directory_name}_{number}"
            directory_name = session_directory.name
        capture = DecisionCapture.from_config(
            {
                "mode": "agent_actions",
                "coverage": "produce_session_segment",
                "session_id": session_id,
                "policy": "existing_auto",
                "reward_language_layout": context.get_node_data("ProduceRewardReceive"),
                "bindings": bindings,
                "entry": entry,
                "task_started_at": (self.task_origins.get(key) or {}).get("task_started_at"),
                "version_files": version_hashes,
            },
            console=logger.info, context=context,
            session_directory=session_directory,
            save_screenshots=bool((context.get_node_data("Produce") or {}).get("attach", {}).get("save_screenshots", False)),
        )
        if not capture:
            self.disabled.add(key)
            return None
        origin = self.task_origins.get(key, {})
        now = started_at or datetime.now(timezone.utc).isoformat()
        state = previous or {
            "schema_version": 4,
            "session_id": session_id,
            "bindings": bindings,
            "started_at": now,
            "start_source": start_source or entry,
            "task_entry_observed": bool(origin),
            "task_started_at": origin.get("task_started_at"),
            "preparation_started_at": origin.get("preparation_started_at"),
            "game_start_observed": fresh_start,
            "gameplay_started_at": now,
            "segments": [],
            "start_observed": fresh_start,
            "settlement_observed": False,
        }
        state["timing_basis"] = "gameplay_loop"
        state.setdefault("start_source", entry)
        state.update(status="recording", root_task_id=task_id, directory_name=directory_name)
        state.setdefault("local_started_at", datetime.fromisoformat(state["started_at"]).astimezone().isoformat())
        state.pop("ended_at", None)
        state.pop("stop_reason", None)
        state["segments"].append({"run_id": capture.run_id, "metadata": capture._manifest["metadata"],
                                  "relative_path": str(capture.directory.relative_to(session_directory)).replace("\\", "/")})
        capture.record(
            "produce_session_resume" if previous else "produce_session_start",
            bindings=bindings,
            continuity="explicit_resume_with_matching_configuration" if previous else "new_or_partial",
            context=context,
        )
        session = {
            "capture": capture,
            "state": state,
            "pointer": pointer,
            "path": session_directory / "data/session.json",
            "pending": [],
            "actions": {},
            "recognitions": {},
            "resumed": bool(previous),
            "fresh_game_started_in_segment": fresh_start,
            "gameplay_entry_recorded": False,
            "settlement_window": bool(state.get("ending_phase_started")),
        }
        self.active[key] = session
        self.persist(session)
        save_json(session_directory / "data/report_pending.json", {"requested_at": now, "reason": "session_open"})
        logger.info(f"培育记录－{'继续本局' if previous else '开始本局'}；回放目录：{directory_name}")
        logger.debug(f"培育会话完整 ID: {session_id}; started_at={state['started_at']}")
        return session

    def persist(self, session):
        save_json(session["path"], session["state"])
        save_json(session["pointer"], session["state"])

    def close(self, key, reason, terminal=False):
        session = self.active.pop(key, None)
        if not session:
            return
        capture, state = session["capture"], session["state"]
        capture.record("produce_session_end", reason=reason, terminal=terminal, settlement_observed=state["settlement_observed"])
        capture.close(reason)
        state.update(status="ended" if terminal else "interrupted", stop_reason=reason, ended_at=datetime.now(timezone.utc).isoformat())
        state["segments"][-1].update(
            stop_reason=reason, capture_status=capture._manifest["status"], events_dropped=capture._manifest.get("events_dropped", 0)
        )
        self.persist(session)
        if terminal:
            session["pointer"].unlink(missing_ok=True)
        from .reporting import schedule_report

        schedule_report(session["path"].parent.parent)

    def close_all(self):
        for key in list(self.active):
            try:
                self.close(key, "agent_shutdown")
            except Exception:
                logger.exception("培育会话关闭失败")


SESSIONS = ProduceSessions()
atexit.register(SESSIONS.close_all)


def safe_image(context):
    try:
        return context.tasker.controller.cached_image
    except Exception:
        return None


class ProduceContextSink(ContextEventSink):
    def on_node_pipeline_node(self, context, noti_type, detail):
        if noti_type != NotificationType.Failed:
            return
        try:
            session = SESSIONS.get(context)
            if session:
                session["capture"].record("pipeline_failure", image=safe_image(context), node=detail.name,
                                          frame_source="last_cached_at_failure", game_action_confirmed=None, context=context)
        except Exception:
            logger.exception("培育异常画面记录失败，继续原失败处理")

    def on_node_wait_freezes(self, context, noti_type, detail):
        if detail.phase != "pre" or detail.name not in (SETTLEMENT_ENTRIES | SETTLEMENT_NAVIGATION):
            return
        if noti_type not in (NotificationType.Starting, NotificationType.Succeeded, NotificationType.Failed):
            return
        try:
            session = SESSIONS.get(context)
            if session and (capture_window(session) or detail.name in SETTLEMENT_ENTRIES):
                if noti_type == NotificationType.Starting:
                    session.get("settlement_pre_wait", {}).pop(detail.name, None)
                    return
                session.setdefault("settlement_pre_wait", {})[detail.name] = {
                    "succeeded": noti_type == NotificationType.Succeeded,
                    "elapsed_ms": detail.elapsed, "roi": list(detail.roi), "parameters": detail.param,
                }
        except Exception:
            logger.exception("培育结算等待记录失败，继续任务")

    def on_node_recognition(self, context, noti_type, detail):
        if noti_type not in (NotificationType.Succeeded, NotificationType.Failed):
            return
        if noti_type == NotificationType.Failed and not any(capture_window(item) for item in list(SESSIONS.active.values())):
            return
        if not SESSIONS.active and detail.name not in PREPARATION_NODES:
            return
        try:
            session = SESSIONS.get(context)
            if session and session["capture"]._analyzing:
                return
            # 在 DirectHit 兜底之前只保留最近一次图像识别编号。
            # 输入前读取一次画面，不在每个未命中的候选节点上重复读取。
            if session and capture_window(session) and noti_type in (NotificationType.Succeeded, NotificationType.Failed):
                if detail.name != "Click_1":
                    session["settlement_latest_recognition"] = detail.reco_id
            if noti_type != NotificationType.Succeeded:
                return
            if not session:
                origin = SESSIONS.task_origins.get(SESSIONS.key(context.tasker))
                if origin and detail.name in PREPARATION_NODES:
                    origin["recognitions"][detail.name] = context.tasker.get_recognition_detail(detail.reco_id)
                return
            if session and (detail.name in (set(ROUTE_NODES) | PREPARATION_NODES | SETTLEMENT_ENTRIES)
                            or (capture_window(session) and detail.name in SETTLEMENT_NAVIGATION)):
                reco = context.tasker.get_recognition_detail(detail.reco_id)
                session["recognitions"][detail.name] = reco
            # DirectHit 不会获取新截图，
            # 不能将其输入前缓存当作操作结果画面。
            if session and session["pending"]:
                reco = context.tasker.get_recognition_detail(detail.reco_id)
                algorithm = getattr(reco, "algorithm", None)
                if getattr(algorithm, "value", algorithm) == "DirectHit":
                    return
                raw = getattr(reco, "raw_image", None)
                image = raw if raw is not None and raw.size else safe_image(context)
                for decision_id in session["pending"]:
                    session["capture"].record(
                        "route_after",
                        image=image,
                        decision_id=decision_id,
                        next_node=detail.name,
                        reco_id=detail.reco_id,
                        game_action_confirmed=None,
                        intervening_route_inputs=len(session["pending"]),
                        observation_phase="next_successful_image_recognition",
                        context=context,
                    )
                session["pending"].clear()
        except Exception:
            logger.exception("培育后续观测记录失败，继续任务")

    def on_node_action(self, context, noti_type, detail):
        if detail.name in SETTLEMENT_NAVIGATION and not any(capture_window(item) for item in list(SESSIONS.active.values())):
            return
        watched = (
            PROCESS_NODES
            | ENTRY_NODES
            | GAMEPLAY_NODES
            | set(ROUTE_NODES)
            | PREPARATION_NODES
            | SETTLEMENT_ENTRIES
            | SETTLEMENT_NAVIGATION
            | {
                "Produce",
                "ProduceLoop",
                "ProduceHomeFlag",
                "ProduceBackHome",
                "ProduceFailedStop",
                "ProduceNIAFailedStop",
                "ProduceFailedFlag",
                "ProduceNIAFailedFlag",
                "ProduceFailedRetry",
                "ProduceNIAFailedRetry",
                "ProduceCardsFlag",
                "ProduceChooseEventFlag",
                "ProduceChooseNIAEventFlag",
            }
        )
        if detail.name not in watched:
            return
        try:
            self.observe(context, noti_type, detail)
        except Exception:
            logger.exception("培育事件采集失败，继续任务")

    def observe(self, context, noti_type, detail):
        name = detail.name
        key = SESSIONS.key(context.tasker)
        session = SESSIONS.get(context)
        if session and name in PROCESS_NODES and name not in ROUTE_NODES:
            if noti_type in (NotificationType.Succeeded, NotificationType.Failed):
                session["capture"].record("pipeline_action", node=name, action_id=detail.action_id,
                                          framework_action_succeeded=noti_type == NotificationType.Succeeded,
                                          game_action_confirmed=None, context=context)
        if noti_type == NotificationType.Starting:
            if name in {"ProduceCardsFlag", "ProduceChooseEventFlag", "ProduceChooseNIAEventFlag", "ProduceChooseGetFlag"}:
                session = SESSIONS.get(context)
                if session:
                    session.pop("exam_result_capture_remaining", None)
                    if not session.get("settlement_window"):
                        session.pop("settlement_latest_recognition", None)
            if name in {"ProduceLoop", "ProduceHomeFlag", "ProduceStart"}:
                session = SESSIONS.get(context)
                if session:
                    session.pop("settlement_window", None)
                    session.pop("settlement_latest_recognition", None)
                    session.pop("settlement_pre_wait", None)
                    session.pop("exam_result_capture_remaining", None)
                    if session["state"].pop("ending_phase_started", None):
                        SESSIONS.persist(session)
            if name == "Produce":
                SESSIONS.disabled.discard(key)  # 每个新任务重新读取采集开关。
                root_task_id = context.get_task_job().job_id
                now = SESSIONS.task_entry_times.get(root_task_id) or datetime.now(timezone.utc).isoformat()
                session = SESSIONS.get(context)
                if session and session["state"]["root_task_id"] != context.get_task_job().job_id:
                    SESSIONS.close(key, "new_task_started")
                SESSIONS.task_origins[key] = {
                    "task_started_at": now, "root_task_id": root_task_id,
                    "entry_mode": None, "preparations": {}, "recognitions": {},
                }
                return
            if name == "ProduceLoop":
                if not SESSIONS.get(context) and key in SESSIONS.task_origins:
                    SESSIONS.task_origins[key].update(entry_mode=None, preparations={}, recognitions={})
                return
            if name in {"ProduceStart", "ProduceContinue", "ProduceSkipPreparation"}:
                origin = SESSIONS.task_origins.get(key)
                if origin:
                    origin["entry_mode"] = {"ProduceStart": "fresh", "ProduceContinue": "resume",
                                            "ProduceSkipPreparation": "skip"}[name]
                    origin["preparation_started_at"] = datetime.now(timezone.utc).isoformat() if name == "ProduceStart" else None
                return
            if name in GAMEPLAY_NODES:
                session = SESSIONS.get(context)
                origin = SESSIONS.task_origins.get(key, {})
                if not session:
                    session = SESSIONS.ensure(context, name, fresh_start=origin.get("entry_mode") == "fresh")
                if session and not session["gameplay_entry_recorded"]:
                    session["gameplay_entry_recorded"] = True
                    capture = session["capture"]
                    for preparation in origin.get("preparations", {}).values():
                        capture.record("preparation_observation", **preparation, buffered_before_gameplay=True, context=context)
                    origin.get("preparations", {}).clear()
                    origin.get("recognitions", {}).clear()
                    capture.record("produce_gameplay_entry", node=name, fresh_start=session["fresh_game_started_in_segment"], context=context)
                return
            session = SESSIONS.get(context)
            if not session:
                if name in PREPARATION_NODES:
                    origin = SESSIONS.task_origins.get(key)
                    if origin:
                        reco = origin["recognitions"].pop(name, None)
                        raw = getattr(reco, "raw_image", None)
                        image = raw if raw is not None and raw.size else safe_image(context)
                        origin["preparations"][name] = {
                            "image": image.copy() if image is not None else None, "node": name,
                            "recognition": getattr(reco, "raw_detail", None),
                            "observed_at": datetime.now(timezone.utc).isoformat(),
                        }
                return
            capture = session["capture"]
            if name in SETTLEMENT_ENTRIES:
                session["settlement_window"] = True
                if not session["state"].get("ending_phase_started"):
                    session["state"]["ending_phase_started"] = True
                    SESSIONS.persist(session)
            if name in SETTLEMENT_NAVIGATION or name == "ProduceEnd":
                if capture_window(session):
                    kind = "settlement_observation" if session.get("settlement_window") else "exam_result_observation"
                    self.capture_settlement(session, name, context, kind)
                    if session.get("exam_result_capture_remaining"):
                        session["exam_result_capture_remaining"] -= 1
                return
            if name in ROUTE_NODES:
                node = context.get_node_data(name)
                reco = session["recognitions"].pop(name, None)
                if name == "ProduceChooseGetFlag":
                    from .rewards import reward_kind
                    session["reward_kind"] = reward_kind(getattr(getattr(reco, "best_result", None), "text", ""))
                raw_image = getattr(reco, "raw_image", None)
                event_id = capture.record(
                    "route_before",
                    image=raw_image if raw_image is not None and raw_image.size else safe_image(context),
                    operation=ROUTE_NODES[name],
                    node=name,
                    action_id=detail.action_id,
                    node_settings=node,
                    recognition=getattr(reco, "raw_detail", None),
                    frame_source="recognition_raw" if raw_image is not None and raw_image.size else "controller_cached_at_action_start",
                    candidates_complete=False,
                    actual_cost=None,
                    game_action_confirmed=None,
                    context=context,
                )
                session["actions"][detail.action_id] = event_id
                if name == "ProduceChooseConfirm":
                    session["reward_confirmation_id"] = event_id
            elif name in SETTLEMENT_NODES:
                self.capture_settlement(session, name, context)
                session["state"]["settlement_observed"] = True
                SESSIONS.persist(session)
            elif name in PREPARATION_NODES:
                reco = session["recognitions"].pop(name, None)
                capture.record("preparation_observation", image=safe_image(context), node=name, recognition=getattr(reco, "raw_detail", None), context=context)
            elif name in {"ProduceFailedFlag", "ProduceNIAFailedFlag", "ProduceFailedRetry", "ProduceNIAFailedRetry"}:
                capture.record("exam_failure_or_retry", image=safe_image(context), node=name, context=context)
            return
        session = SESSIONS.get(context)
        if not session:
            return
        if name == "ProduceCardsFlag" and noti_type == NotificationType.Succeeded:
            # 初/NIA 成功或失败都可能先显示结果，再直接进入回忆生成。
            # 旁路观察原有翻页；下一场战斗、路线或奖励会清除此有限窗口。
            session["exam_result_capture_remaining"] = 16
            return
        if detail.action_id in session["actions"]:
            decision = session["actions"].pop(detail.action_id)
            action = context.tasker.get_action_detail(detail.action_id)
            if name == "ProduceChooseConfirm":
                action = session.pop("reward_receive_action", None) or action
            input_id = session["capture"].record(
                "route_input_result",
                decision_id=decision,
                node=name,
                action_id=detail.action_id,
                framework_action_succeeded=noti_type == NotificationType.Succeeded,
                action_detail=getattr(action, "raw_detail", None),
                actual_point=list(action.result.point) if action and getattr(action.result, "point", None) else None,
                actual_box=list(action.box) if action else None,
                game_action_confirmed=None,
                context=context,
            )
            if noti_type == NotificationType.Succeeded and name in {
                "ProduceChooseFirst", "ProduceChooseRecommend", "ProduceChooseEventRecommend", "ProduceChooseSkip",
            }:
                session["reward_choice"] = {
                    "decision_id": decision, "input_event_id": input_id, "policy_node": name,
                    "actual_point": list(action.result.point) if action and getattr(action.result, "point", None) else None,
                }
            if decision:
                session["pending"].append(decision)
        if noti_type == NotificationType.Succeeded:
            if name in {"ProduceFailedStop", "ProduceNIAFailedStop"}:
                session["state"]["failure_stop_observed"] = True
                SESSIONS.persist(session)
            elif name == "ProduceBackHome":
                SESSIONS.close(
                    key, "failure_returned_home" if session["state"].get("failure_stop_observed") else "returned_home", terminal=True
                )

    @staticmethod
    def capture_settlement(session, name, context, kind="settlement_observation"):
        reco = session["recognitions"].pop(name, None)
        algorithm = getattr(reco, "algorithm", None)
        raw = getattr(reco, "raw_image", None)
        source = "recognition_raw"
        reco_id = getattr(reco, "reco_id", None)
        freeze = session.get("settlement_pre_wait", {}).pop(name, None)
        waited_image = safe_image(context) if freeze is not None else None
        if waited_image is not None and waited_image.size:
            # Maa 在 pre_wait_freezes 之后发出 Action.Starting，
            # 此时识别原图已经较旧，应读取等待后的最新缓存画面。
            raw = waited_image
            source = "controller_cached_after_pre_wait_freezes"
            session.pop("settlement_latest_recognition", None)
        elif reco and getattr(algorithm, "value", algorithm) != "DirectHit" and (raw is None or not raw.size):
            raw = safe_image(context)
            source = "controller_cached_before_image_node_action"
        has_waited_image = waited_image is not None and bool(waited_image.size)
        if not has_waited_image and (getattr(algorithm, "value", algorithm) == "DirectHit" or raw is None or not raw.size):
            latest_id = session.pop("settlement_latest_recognition", None)
            if not latest_id:
                return  # 输入前的旧缓存不作为新观察。
            reco = context.tasker.get_recognition_detail(latest_id)
            algorithm = getattr(reco, "algorithm", None)
            if not reco or getattr(algorithm, "value", algorithm) == "DirectHit":
                return
            reco_id, raw = latest_id, getattr(reco, "raw_image", None)
            source = "latest_image_recognition_before_fallback"
            if raw is None or not raw.size:
                raw = safe_image(context)
                source = "controller_cached_after_image_recognition_before_fallback"
            if raw is None or not raw.size:
                return
        else:
            session.pop("settlement_latest_recognition", None)
        digest = hashlib.sha256(raw.tobytes()).hexdigest()
        if digest == session.get("settlement_last_frame"):
            return
        session["settlement_last_frame"] = digest
        session["capture"].record(
            kind, image=raw, node=name, reco_id=reco_id,
            recognition=getattr(reco, "raw_detail", None), frame_source=source,
            wait_freezes=freeze,
            observation_phase="ending_navigation_before_input" if kind == "settlement_observation" else "post_battle_navigation_before_input",
            evaluation_page_confirmed=None,
            context=context,
        )


class ProduceTaskSink(TaskerEventSink):
    def on_tasker_task(self, tasker, noti_type, detail):
        if noti_type == NotificationType.Starting:
            if detail.entry == "Produce":
                SESSIONS.task_entry_times[detail.task_id] = datetime.now(timezone.utc).isoformat()
            return
        try:
            # 客户端发送任务结束通知时，Agent 回调不能安全地反向查询控制器，
            # 因此使用之前保存的任务编号关联收尾。
            for key, session in list(SESSIONS.active.items()):
                if detail.task_id == session["state"]["root_task_id"]:
                    failed = bool(session["state"].get("failure_stop_observed"))
                    SESSIONS.close(key, "failure_terminated" if failed else "task_ended_without_home", terminal=failed)
            for key, origin in list(SESSIONS.task_origins.items()):
                if origin.get("root_task_id") == detail.task_id:
                    SESSIONS.task_origins.pop(key, None)
            SESSIONS.task_entry_times.pop(detail.task_id, None)
        except Exception:
            logger.exception("培育会话收尾失败，继续任务")


def install_produce_capture():
    from maa.agent.agent_server import AgentServer

    from .reporting import recover_reports, notify_ready_reports

    recover_reports(ROOT / "debug/produce_sessions")
    notify_ready_reports(ROOT / "debug/produce_sessions", logger.info)
    AgentServer.add_context_sink(ProduceContextSink())
    AgentServer.add_tasker_sink(ProduceTaskSink())
