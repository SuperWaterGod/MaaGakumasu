"""对已采集的对局和路线画面执行内存图像 OCR 与分析，不发送游戏输入。"""

import os
import re
import sys
import json
import math
import time
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone
from importlib.metadata import version

# 内置 Python 不自动加入脚本目录，在入口设置离线命令行工具的导入路径。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from produce.state import STANCE_NAMES, STATE_FIELDS, ANOMALY_FIELDS, BattleStateReader
from utils.image_io import bgr_image, read_image, resize_image
from produce.catalog import SkillCardCatalog, clean, normalize_name
from produce.console import RewardConsole, OperationConsole, SettlementConsole, state_lines, hand_summary
from produce.rewards import RewardReader
from produce.reporting import read_json
from produce.settlement import PARAMETER_ICONS, FinalExamTracker, settlement_observation


def normalized_image(image):
    image = bgr_image(image)
    height, width = image.shape[:2]
    if abs(width / height - 720 / 1280) > 0.005:
        raise ValueError("Unsupported frame aspect ratio; will not rotate")
    return resize_image(image, 720, 1280)


def labeled_value(tokens, labels, pattern):
    values = []
    evidence = []
    for token in tokens:
        if token.get("confidence", 0) < 0.8:
            continue
        text = clean(token["text"])
        label = re.search(labels, text, re.IGNORECASE)
        if not label:
            continue
        tail = text[label.end() :].strip(" ：:")
        value = re.fullmatch(pattern, tail)
        if value:
            values.append(value.group())
            evidence.append(token)
            continue
        # 独立数值须与标签位于同一行，且在标签右侧。
        x, y, w, h = token["box"]
        nearby = [
            other
            for other in tokens
            if other is not token
            and other.get("confidence", 0) >= 0.8
            and x + w <= other["box"][0] <= x + w + 150
            and abs(other["box"][1] - y) <= max(10, h // 2)
            and re.fullmatch(pattern, clean(other["text"]))
        ]
        if len(nearby) == 1:
            values.append(clean(nearby[0]["text"]))
            evidence.extend([token, nearby[0]])
    unique = {int(value.replace(",", "")) if re.fullmatch(r"[0-9][0-9,]*", value) else value for value in values}
    return {
        "value": next(iter(unique)) if len(unique) == 1 else None,
        "evidence": evidence,
        "status": "read" if len(unique) == 1 else "unknown",
    }


def route_observation(tokens, catalog, event):
    candidates = []
    for index, token in enumerate(tokens):
        identity = catalog.match(token["text"], token.get("confidence", 0))
        # 路线描述也可能包含效果文本，只接受完整匹配的库名称。
        if identity["status"] == "unknown" or identity.get("match_method") != "exact":
            continue
        candidates.append(
            {
                "candidate_id": f"ocr_{index}",
                "name_box": token["box"],
                "identity": identity,
                "cost": {"value": None, "unit": None, "evidence": [], "status": "unknown"},
                "available": None,
                "ocr": token,
            }
        )
    price_tokens = []
    for token in tokens:
        match = re.fullmatch(r"(?:必要|消費|花费|价格|費用)?\s*([0-9][0-9,]*)\s*(?:P\s*ポイント|ポイント|Pt|PT|点)", clean(token["text"]))
        if match and token.get("confidence", 0) >= 0.8:
            price_tokens.append({"value": int(match[1].replace(",", "")), "unit": "produce_points", "evidence": [token]})
    # 只关联与唯一卡名处于同一行的明确价格。
    # 裸数字和商店余额保留为原始 OCR 证据。
    if event.get("operation", "").startswith("shop"):
        for card in candidates:
            cx, cy, cw, ch = card["name_box"]
            owned_prices = [
                price
                for price in price_tokens
                if abs(price["evidence"][0]["box"][1] - cy) <= max(price["evidence"][0]["box"][3], ch)
                and 0 <= price["evidence"][0]["box"][0] - (cx + cw) <= 180
            ]
            if len(owned_prices) == 1:
                price = owned_prices[0]
                owners = [
                    other
                    for other in candidates
                    if abs(other["name_box"][1] - price["evidence"][0]["box"][1]) <= max(ch, other["name_box"][3])
                    and 0 <= price["evidence"][0]["box"][0] - (other["name_box"][0] + other["name_box"][2]) <= 180
                ]
                if len(owners) == 1:
                    card["cost"] = {**price, "status": "explicit_same_row"}
            elif len(owned_prices) > 1:
                card["cost"]["status"] = "conflicting_prices"
                card["cost"]["evidence"] = [token for price in owned_prices for token in price["evidence"]]
    settlement = None
    if event["kind"] == "settlement_observation":
        settlement = settlement_observation(tokens)["settlement"]
    return {
        "schema_version": 1,
        "operation": event.get("operation"),
        "node": event.get("node"),
        "candidates": candidates,
        "candidates_complete": False,
        "candidate_scope": "exact_catalog_names_visible_in_ocr; other_items_in_raw_ocr",
        "explicit_prices": price_tokens,
        "actual_cost": None,
        "levels": {
            "idol_level": labeled_value(tokens, r"(?:親愛度|好感度|偶像等级)", r"[0-9]+"),
            "player_level": labeled_value(tokens, r"(?:PLv\.?|Pレベル|玩家等级)", r"[0-9]+"),
        }
        if event["kind"] == "preparation_observation"
        else None,
        "deck_observation": {
            "cards": [card["identity"] for card in candidates],
            "complete": False,
            "scope": "visible_names; route_candidates_are_not_owned_cards",
        },
        "settlement": settlement,
        "raw_ocr": tokens,
        "game_action_confirmed": None,
    }


def selection_evidence(observation, action):
    point = action.get("actual_point")
    if not point:
        return {"candidate_id": None, "status": "unknown", "actual_point": None}
    x, y = point
    # 点击下方的牌名只表示几何位置对应，不确认库存变化。
    candidates = []
    for card in observation.get("candidates", []):
        left, top, width, height = card["name_box"]
        if left - 40 <= x <= left + width + 40 and top - 240 <= y <= top + height:
            candidates.append(card)
    return {
        "candidate_id": candidates[0]["candidate_id"] if len(candidates) == 1 else None,
        "status": "geometry_only" if len(candidates) == 1 else "unknown",
        "actual_point": point,
        "actual_box": action.get("actual_box"),
        "framework_action_succeeded": action.get("framework_action_succeeded"),
        "game_action_confirmed": None,
    }


class OfflineVision:
    def __init__(self, resource_path, context=None):
        self.context = context
        if context is not None:
            self._ocr_cache = {}
            return
        import numpy as np
        from maa.define import LoggingLevelEnum
        from maa.tasker import Tasker
        from maa.resource import Resource
        from maa.controller import CustomController

        # 独立 OCR 进程不初始化 Toolkit 的落盘日志，也不自动保存识别绘图。
        Tasker.set_log_dir("")
        Tasker.set_stdout_level(LoggingLevelEnum.Error)
        Tasker.set_save_draw(False)
        Tasker.set_save_on_error(False)
        self.resource = Resource()
        if not self.resource.post_bundle(resource_path).wait().status.succeeded:
            raise RuntimeError("Cannot load analysis resource")
        # 使用内存控制器提供图像，所有游戏输入回调均拒绝执行。
        methods = {
            name: (lambda self, *args: False)
            for name in dir(CustomController)
            if getattr(getattr(CustomController, name), "__isabstractmethod__", False)
        }
        methods.update(
            connect=lambda self: True,
            request_uuid=lambda self: "capture-offline-analysis",
            screencap=lambda self: np.zeros((1280, 720, 3), dtype=np.uint8),
        )
        self.controller = type("InputFreeController", (CustomController,), methods)()
        if not self.controller.post_connection().wait().status.succeeded:
            raise RuntimeError("Cannot initialize offline image provider")
        self.tasker = Tasker()
        if not self.tasker.bind(self.resource, self.controller):
            raise RuntimeError("Cannot bind offline recognizer")
        self._ocr_cache = {}

    def recognize(self, kind, param, image):
        if self.context is not None:
            context = self.context() if callable(self.context) else self.context
            if context is None:
                raise RuntimeError("当前记录缺少有效识别 Context")
            detail = context.run_recognition_direct(kind, param, image)
            return detail.all_results if detail else []
        job = self.tasker.post_recognition(kind, param, image).wait()
        if not job.status.succeeded:
            raise RuntimeError(f"Recognition job failed: {kind}")
        detail = job.get()
        if not detail or not detail.nodes or not detail.nodes[0].recognition:
            raise RuntimeError(f"No recognition detail: {kind}")
        return detail.nodes[0].recognition.all_results

    def _read_title(self, array, roi):
        from maa.pipeline import JOCR, JRecognitionType

        left, top, width, height = roi
        crop_hash = hashlib.sha256(array[top : top + height, left : left + width].tobytes()).hexdigest()
        if crop_hash not in self._ocr_cache:
            results = self.recognize(JRecognitionType.OCR, JOCR(roi=roi), array)
            self._ocr_cache[crop_hash] = [
                {"text": item.text, "confidence": item.score, "box": [item.box[0] - left, item.box[1] - top, item.box[2], item.box[3]]}
                for item in results
            ]
            if len(self._ocr_cache) > 2048:
                self._ocr_cache.pop(next(iter(self._ocr_cache)))
        return [{**item, "box": [item["box"][0] + left, item["box"][1] + top, *item["box"][2:]]} for item in self._ocr_cache[crop_hash]]

    def match_buff_templates(self, array, definition):
        from maa.pipeline import JTemplateMatch, JRecognitionType

        items = self.recognize(
            JRecognitionType.TemplateMatch,
            JTemplateMatch(
                template=[definition["template"]],
                roi=definition["roi"],
                threshold=[definition["threshold"]],
                method=definition["method"],
                green_mask=True,
            ),
            array,
        )
        return [{"score": item.score, "box": list(item.box)} for item in items if math.isfinite(item.score)]

    def analyze(self, image, catalog, candidates=None):
        from maa.pipeline import JRecognitionType, JNeuralNetworkDetect

        array = normalized_image(image)
        if candidates is None:
            detections = self.recognize(
                JRecognitionType.NeuralNetworkDetect,
                JNeuralNetworkDetect(model="cards.onnx", labels=["cards", "suggestions", "useless"], expected=[0, 1, 2]),
                array,
            )
            candidates = [
                {
                    "candidate_id": f"detection_{i}",
                    "box": list(item.box),
                    "label": item.label,
                    "detector_confidence": item.score,
                    "in_hand_region": 840 <= item.box[1] <= 1000,
                }
                for i, item in enumerate(detections)
            ]
        cards = []
        for candidate in candidates:
            if not candidate.get("in_hand_region"):
                continue
            x, y, width, height = map(int, candidate["box"])
            # ADB 检测框须与原图缩放一致，Agent 候选框要求图像为 720×1280。
            left, top = max(0, x), max(0, y + int(height * 0.80))
            right, bottom = min(720, x + width), min(1280, y + height)
            if right <= left or bottom <= top:
                continue
            roi = (left, top, right - left, bottom - top)
            visible_right = right
            for neighbor in candidates:
                nx, ny, nw, nh = neighbor["box"]
                if neighbor.get("in_hand_region") and nx >= x + width * 0.25 and nw >= width * 0.65 and ny <= (top + bottom) / 2 <= ny + nh:
                    visible_right = min(visible_right, int(nx))
            overlap = visible_right < right and visible_right - left >= 30
            rois = [(left, top, visible_right - left, bottom - top), roi] if overlap else [roi]
            views = []
            for title_roi in rois:
                texts = self._read_title(array, title_roi)
                # 相邻卡名的起点位于当前卡牌可见区域之外时，不将其整段选入。
                eligible = [item for item in texts if not overlap or item["box"][0] < visible_right]
                title = max(eligible, key=lambda item: len(normalize_name(item["text"])), default=None)
                identity = catalog.match(title["text"], title["confidence"], allow_overlap=overlap) if title else catalog.match("")
                views.append({"roi": list(title_roi), "ocr_results": texts, "identity": identity})
            accepted = [view for view in views if view["identity"]["database_card_id"]]
            if accepted:
                # 裁切可能漏掉第二个加号，保留已观察到的最强强化标记证据。
                chosen = max(accepted, key=lambda view: view["identity"]["observed_plus_count"])
            else:
                chosen = max(views, key=lambda view: view["identity"].get("name_similarity", 0))
            identity = dict(chosen["identity"])
            if len({view["identity"]["database_card_id"] for view in accepted}) > 1:
                base_names = {normalize_name(view["identity"]["database_name"]).rstrip("+") for view in accepted}
                if len(base_names) == 1:
                    identity.update(
                        status="base_match_variant_unresolved",
                        card_id=None,
                        unknown_reason="inconsistent_upgrade_visibility",
                        variant_evidence_conflict=True,
                        upgrade_status="unresolved",
                        upgraded=None,
                    )
                else:
                    identity.update(status="unknown", card_id=None, database_card_id=None, unknown_reason="conflicting_ocr_views")
            cards.append(
                {
                    **candidate,
                    "title_roi": chosen["roi"],
                    "ocr_results": chosen["ocr_results"],
                    "identity": identity,
                    "overlap_pixels": right - visible_right if overlap else 0,
                    "ocr_views": views,
                }
            )
        result = {"analysis_size": [720, 1280], "source_size": [image.shape[1], image.shape[0]], "cards": cards}
        if getattr(self, "state_reader", None):
            try:
                result["battle_state"] = self.state_reader.read(array)
            except Exception as exc:
                result["battle_state"] = {"hud_present": False, "error": str(exc), "fields": {}, "buffs": []}
        return result


def analyze_stream(directory, resource, database, aliases=None, *, context=None, emit=None):
    directory = directory.resolve()
    catalog = SkillCardCatalog(database, aliases)
    reward_reader = RewardReader(catalog, database.with_name("produce_reward_items.json"), directory)
    model_sha = hashlib.sha256((resource / "model/detect/cards.onnx").read_bytes()).hexdigest()
    state_templates = database.with_name("battle_buff_templates.json")
    signature = "current"
    output = directory / "analysis"
    output.mkdir(parents=True, exist_ok=True)
    status = {
        "schema_version": 1,
        "pid": os.getpid(),
        "status": "starting",
        "analysis_id": signature,
        "catalog_sha256": catalog.sha256,
        "catalog_source": catalog.source,
        "model_sha256": model_sha,
        "maafw_package_version": version("maafw"),
        "unique_frames_processed": 0,
        "event_links": 0,
        "matched": 0,
        "variant_unresolved": 0,
        "unknown": 0,
        "errors": 0,
        "state_errors": 0,
        "state_frames": 0,
        "state_field_observations": {key: 0 for key in STATE_FIELDS},
        "anomaly_frames": 0,
        "anomaly_field_observations": {key: 0 for key in ANOMALY_FIELDS},
        "route_frames": 0,
        "route_errors": 0,
        "settlement_frames": 0,
        "exam_result_frames": 0,
        "settlement_field_observations": {key: 0 for key in ("score", "rank", "Vo", "Da", "Vi")},
        "reward_preview_frames": 0,
        "reward_preview_errors": 0,
        "reward_observation_schema_version": 2,
        "reward_effect_statuses": {},
        "reward_catalog_hashes": reward_reader.catalog.hashes,
        "reward_item_source": reward_reader.catalog.item_source,
        "reward_item_error": reward_reader.catalog.item_error,
        "reward_reading_counts": reward_reader.counts,
    }
    status_path = output / "status.json"
    capture_manifest = read_json(directory / "manifest.json")

    def notice(message, **extra):
        if emit:
            emit(message, **extra)

    def save_status():
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        from produce.reporting import atomic_json
        atomic_json(status_path, status)

    action_console = OperationConsole()
    reward_console = RewardConsole()
    bindings = capture_manifest.get("metadata", {}).get("bindings", {})
    settlement_console = SettlementConsole(bindings)
    exam_tracker = FinalExamTracker(bindings.get("scenario"))
    try:
        save_status()
        vision = OfflineVision(resource, context)
        if reward_reader.icons:
            reward_reader.icons.recognize = vision.recognize
        vision.state_reader = BattleStateReader(state_templates, vision._read_title, vision.match_buff_templates, resource / "image")
        status["buff_backend"] = "MaaFramework.TemplateMatch"
        status["buff_templates"] = [entry["id"] for entry in vision.state_reader.templates if entry["kind"] == "buff"]
        status["stance_templates"] = [entry["id"] for entry in vision.state_reader.templates if entry["kind"] == "stance"]
        status["stance_preview_templates"] = [entry["id"] for entry in vision.state_reader.templates if entry["kind"] == "stance_preview"]
        status["buff_template_errors"] = vision.state_reader.template_error
        if vision.state_reader.template_error:
            notice("部分 Buff 模板不可用，未确认的状态保留未知：" + vision.state_reader.template_error)
        from maa.library import Library

        status["framework_version"] = Library.version()
        status["status"] = "processing"
        save_status()
        cached = {}
        route_pending = {}
        last_summary = None
        with (output / "results.jsonl").open("w", encoding="utf-8", buffering=1) as index:
            while True:
                packet = yield
                if packet is None:
                    break
                event, supplied_image = packet
                icon_matches = None
                if (reward_reader.icons and supplied_image is not None
                        and (event["kind"] == "route_before" or event["kind"] == "move_cards_screen")
                        and event.get("operation") in {"shop", "strengthen", "delete_copy_exchange", "move_cards"}):
                    try:
                        matches = reward_reader.icons.scan(normalized_image(supplied_image), event["operation"])
                        icon_matches = matches
                        if event["kind"] == "move_cards_screen":
                            index.write(json.dumps({"event_id": event["event_id"], "icon_matches": matches,
                                                    "game_action_confirmed": None}, ensure_ascii=False) + "\n")
                        notice("页面图标识别", scope="icons", event_id=event["event_id"],
                               operation=event["operation"], icon_matches=matches)
                    except Exception:
                        from utils import logger
                        logger.exception("页面图标缓存识别失败，继续原流程")
                exam_tracker.observe(event)
                if event["kind"] == "produce_session_end":
                    summary = settlement_console.finish(event.get("event_id"))
                    if summary:
                        status["evaluation_check"] = summary["evaluation_check"]
                        notice(**summary)
                    continue
                if event["kind"].startswith("reward_") and event["kind"] != "reward_candidate_preview":
                    summary = reward_console.observe(event)
                    if summary:
                        notice(**summary)
                    continue
                if event["kind"] == "reward_candidate_preview":
                    result = {"error": "missing_frame", "detail_verified": False}
                    try:
                        array = normalized_image(supplied_image)
                        result = reward_reader.read(array, event, vision._read_title)
                        result.update(source_frame_sha256=event["frame"]["sha256"], catalog_sha256=catalog.sha256)
                        semantic_status = result["effects"]["semantic_status"]
                        status["reward_effect_statuses"][semantic_status] = status["reward_effect_statuses"].get(semantic_status, 0) + 1
                        status["reward_preview_frames"] += 1
                    except Exception as exc:
                        result["error"] = str(exc)
                        status["reward_preview_errors"] += 1
                        from utils import logger
                        logger.exception("奖励名称识别异常")
                    index.write(json.dumps({
                        "event_id": event["event_id"], "session_id": event.get("session_id"),
                        "reward_page_id": event.get("reward_page_id"),
                        "candidate_id": event.get("candidate", {}).get("candidate_id"),
                        "observation": result,
                    }, ensure_ascii=False) + "\n")
                    status["event_links"] += 1
                    reward_console.observe(event, result)
                    save_status()
                    continue
                if event["kind"] == "route_input_result":
                    before = route_pending.pop(event.get("decision_id"), {})
                    selection = selection_evidence(before, event)
                    index.write(json.dumps({
                        "event_id": event["event_id"], "session_id": event.get("session_id"),
                        "decision_id": event.get("decision_id"), "selection": selection,
                        "game_action_confirmed": None,
                    }, ensure_ascii=False) + "\n")
                    status["event_links"] += 1
                    continue
                if event["kind"] in ("route_before", "route_after", "preparation_observation", "settlement_observation", "exam_result_observation"):
                    result = {"error": "missing_frame", "candidates": []}
                    try:

                        array = normalized_image(supplied_image)
                        tokens = vision._read_title(array, [0, 0, 720, 1280])
                        result = route_observation(tokens, catalog, event)
                        if reward_reader.icons and event["kind"] == "route_before":
                            try:
                                learned = reward_reader.icons.learn(array, event, result)
                                if learned:
                                    result["selected_icon_cache_write"] = learned
                            except Exception:
                                from utils import logger
                                logger.exception("已选卡牌图标缓存学习失败，继续原流程")
                        if icon_matches is not None:
                            result["icon_matches"] = icon_matches
                        if event["kind"] in ("settlement_observation", "exam_result_observation"):
                            result.update(settlement_observation(tokens, lambda roi: vision._read_title(array, roi),
                                                                 lambda definition: vision.match_buff_templates(array, definition)))
                            status["settlement_frames" if event["kind"] == "settlement_observation" else "exam_result_frames"] += 1
                            for key, reading in result["settlement"].items():
                                status["settlement_field_observations"][key] += reading["value"] is not None
                        result.update(source_frame_sha256=event["frame"]["sha256"], catalog_sha256=catalog.sha256)
                        status["route_frames"] += 1
                    except Exception as exc:
                        result["error"] = str(exc)
                        status["route_errors"] += 1
                    if event["kind"] == "route_before":
                        route_pending[event["event_id"]] = result
                    if event["kind"] in ("settlement_observation", "exam_result_observation"):
                        exam_tracker.observe(event, result)
                        settlement_console.observe(event, result)
                    index.write(json.dumps({
                        "event_id": event["event_id"], "session_id": event.get("session_id"),
                        "decision_id": event.get("decision_id"),
                        "observation": result,
                        "game_action_confirmed": None,
                    }, ensure_ascii=False) + "\n")
                    status["event_links"] += 1
                    save_status()
                    continue
                if event["kind"] == "decision_after" and "frame" not in event:
                    message, changes, number = action_console.finish(event, {"error": "本场最后一次操作，无后续出牌前画面"})
                    notice(message, event_id=event["event_id"], decision_id=event.get("decision_id"), console_operation_number=number)
                    continue
                if "frame" not in event or event["kind"] not in (
                    "decision_before",
                    "decision_after",
                    "screen_observation",
                    "battle_entry",
                    "battle_exit_observation",
                ):
                    continue
                if event["kind"] == "screen_observation" and event.get("battle_hint") is not True:
                    continue
                sha = event["frame"]["sha256"]
                # 操作前的检测候选优于重新检测结果，按原始事件分别保留。
                key = event.get("frame_reference") or (event["event_id"] if event["kind"] == "decision_before" else sha)
                result_ref = cached.get(key, (None, None))[1]
                if key not in cached:
                    try:
                        image = supplied_image
                        source_size = event["frame"].get("source_size", [image.shape[1], image.shape[0]])
                        candidates = event.get("candidates") if source_size == [720, 1280] else None
                        result = vision.analyze(image, catalog, candidates)
                        result["capture_source_size"] = source_size
                        result.update(schema_version=1, source_frame_sha256=sha, catalog_sha256=catalog.sha256)
                        for card in result["cards"]:
                            value = card["identity"]["status"]
                            status[{"matched": "matched", "base_match_variant_unresolved": "variant_unresolved"}.get(value, "unknown")] += 1
                        state = result.get("battle_state", {})
                        status["state_errors"] += bool(state.get("error") or state.get("errors"))
                        if state.get("hud_present"):
                            status["state_frames"] += 1
                            for field, value in state["fields"].items():
                                status["state_field_observations"][field] += value["value"] is not None
                            anomaly = state.get("anomaly", {})
                            if anomaly.get("hud_present"):
                                status["anomaly_frames"] += 1
                            for field in ANOMALY_FIELDS:
                                status["anomaly_field_observations"][field] += anomaly.get(field, {}).get("value") is not None
                    except Exception as exc:
                        result = {"schema_version": 1, "error": str(exc), "cards": [], "source_frame_sha256": sha}
                        status["errors"] += 1
                    cached[key] = (result, event["event_id"])
                    if len(cached) > 128:
                        cached.pop(next(iter(cached)))
                    status["unique_frames_processed"] += 1
                else:
                    result = cached[key][0]
                changes = None
                console_operation_number = None
                if event["kind"] == "decision_before":
                    action_console.start(event, result)
                    console_operation_number = action_console.count
                elif event["kind"] == "decision_after":
                    message, changes, console_operation_number = action_console.finish(event, result)
                    notice(
                        message,
                        event_id=event["event_id"],
                        decision_id=event.get("decision_id"),
                        console_operation_number=console_operation_number,
                    )
                elif result.get("error"):
                    notice(f"本帧识别失败（#{event['seq']}）：{result['error']}", event_id=event["event_id"])
                elif event["kind"] == "screen_observation":
                    summary = "\n".join([*state_lines(result.get("battle_state")), "手牌：", hand_summary(result["cards"])])
                    if summary != last_summary:
                        notice(f"旁路观察 #{event['seq']}\n{summary}", event_id=event["event_id"])
                        last_summary = summary
                index.write(
                    json.dumps(
                        {
                            "event_id": event["event_id"],
                            "session_id": event.get("session_id"),
                            "battle_id": event.get("battle_id"),
                            "frame_sha256": sha,
                            **({"result_ref": result_ref} if result_ref else {"observation": result}),
                            "decision_id": event.get("decision_id"),
                            "state_changes": changes,
                            "game_action_confirmed": None,
                            "console_operation_number": console_operation_number,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
                status["event_links"] += 1
                if status["event_links"] % 5 == 0:
                    save_status()
        for message, event_id, number in action_console.unfinished():
            notice(message, decision_id=event_id, console_operation_number=number)
        for summary in reward_console.unfinished():
            notice(**summary)
        summary = settlement_console.finish()
        if summary:
            status["evaluation_check"] = summary["evaluation_check"]
            notice(**summary)
        status["status"] = "completed"
        notice(
            f"分析结束：牌名覆盖 {status['matched'] + status['variant_unresolved']}，未知 {status['unknown']}；"
            f"可读状态 {status['state_frames']} 帧，状态识别异常 {status['state_errors']} 帧。",
            finished=True,
        )
    except Exception as exc:
        status.update(status="failed", error=str(exc))
        for message, event_id, number in action_console.unfinished():
            notice(message, decision_id=event_id, console_operation_number=number)
        for summary in reward_console.unfinished():
            notice(**summary)
        notice(f"记录分析失败：{exc}；培育继续。", finished=True)
        raise
    finally:
        save_status()
    return status


def analyze_capture(directory, resource, database, aliases=None, follow=False, max_frames=None):
    """离线 CLI 读取已有证据；客户端不经由磁盘传递待识别截图。"""
    stream = analyze_stream(directory, resource, database, aliases)
    next(stream)
    frames = 0
    with (directory / "events.jsonl").open(encoding="utf-8") as source:
        while True:
            position = source.tell()
            line = source.readline()
            if not line or not line.endswith("\n"):
                source.seek(position)
                if not follow or read_json(directory / "manifest.json")["status"] != "recording":
                    break
                time.sleep(.5)
                continue
            event = json.loads(line)
            image = None
            if event.get("frame", {}).get("path"):
                path = (directory / event["frame"]["path"]).resolve()
                if not path.is_relative_to(directory.resolve()):
                    raise ValueError("Frame escaped capture directory")
                image = read_image(path)
                frames += 1
            stream.send((event, image))
            if max_frames and frames >= max_frames:
                break
    try:
        stream.send(None)
    except StopIteration as finished:
        return finished.value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--resource", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--aliases", type=Path)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--max-frames", type=int)
    args = parser.parse_args()
    analyze_capture(args.capture_dir, args.resource, args.database, args.aliases, args.follow, args.max_frames)


if __name__ == "__main__":
    main()
