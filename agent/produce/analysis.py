"""Input-free background OCR and analysis of captured battles and route observations."""

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

# Embedded Python ignores the script directory; allow direct worker/CLI launch.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from produce.state import STANCE_NAMES, STATE_FIELDS, ANOMALY_FIELDS, BattleStateReader
from utils.image_io import bgr_image, read_image, resize_image
from produce.catalog import SkillCardCatalog, clean, normalize_name
from produce.console import RewardConsole, OperationConsole, state_lines, hand_summary
from produce.rewards import RewardReader


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
        # Only accept a separate value on the same row and to the right of the label.
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
        # Route descriptions also contain effect text. Require an exact catalog name.
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
    # Attach only explicit prices in the same row as one unique card name.
    # Bare digits and shop balance are deliberately left as raw OCR.
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
        settlement = {
            "score": labeled_value(tokens, r"(?:最終スコア|最終評価点|最终得分|最終得点|評価点|总得分)", r"[0-9][0-9,]*"),
            "rank": labeled_value(tokens, r"(?:評価ランク|最终评级|最終評価|评价等级)", r"(?:S{1,3}|[A-D])\+?"),
            "Vo": labeled_value(tokens, r"(?:ボーカル|声乐|Vo)", r"[0-9][0-9,]*"),
            "Da": labeled_value(tokens, r"(?:ダンス|舞蹈|Da)", r"[0-9][0-9,]*"),
            "Vi": labeled_value(tokens, r"(?:ビジュアル|形象|Vi)", r"[0-9][0-9,]*"),
        }
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
    # A name below the click is only a geometric suggestion, never inventory confirmation.
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
    def __init__(self, resource_path, log_directory):
        import numpy as np
        from maa.define import LoggingLevelEnum
        from maa.tasker import Tasker
        from maa.toolkit import Toolkit
        from maa.resource import Resource
        from maa.controller import CustomController

        Toolkit.init_option(str(log_directory))
        # OCR worker evidence is saved in derived JSON and analysis.log. Native
        # per-recognition trace logs otherwise grow by many MB for every replay.
        Tasker.set_log_dir("")
        Tasker.set_stdout_level(LoggingLevelEnum.Error)
        self.resource = Resource()
        if not self.resource.post_bundle(resource_path).wait().status.succeeded:
            raise RuntimeError("Cannot load analysis resource")
        # No physical controller is created. All input callbacks are denied.
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
            # ADB boxes never come from a differently scaled image. Agent candidates require 720x1280.
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
                # Do not select a whole neighboring title which starts outside this card's visible part.
                eligible = [item for item in texts if not overlap or item["box"][0] < visible_right]
                title = max(eligible, key=lambda item: len(normalize_name(item["text"])), default=None)
                identity = catalog.match(title["text"], title["confidence"], allow_overlap=overlap) if title else catalog.match("")
                views.append({"roi": list(title_roi), "ocr_results": texts, "identity": identity})
            accepted = [view for view in views if view["identity"]["database_card_id"]]
            if accepted:
                # A clipped + may lose the second marker. Preserve the strongest visible variant evidence.
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


def analyze_capture(directory, resource, database, aliases=None, follow=False, max_frames=None):
    directory = directory.resolve()
    catalog = SkillCardCatalog(database, aliases)
    reward_reader = RewardReader(catalog, database.with_name("produce_reward_items.json"), directory)
    model_sha = hashlib.sha256((resource / "model/detect/cards.onnx").read_bytes()).hexdigest()
    state_templates = database.with_name("battle_buff_templates.json")
    state_reader = BattleStateReader(state_templates, None, image_root=resource / "image")
    signature = hashlib.sha256(
        (
            catalog.sha256
            + model_sha
            + version("maafw")
            + (hashlib.sha256(aliases.read_bytes()).hexdigest() if aliases and aliases.exists() else "")
            + "".join(path.read_text(encoding="utf-8") for path in sorted(Path(__file__).parent.glob("*.py")))
            + Path(__file__).parents[1].joinpath("utils/image_io.py").read_text(encoding="utf-8")
            + (reward_reader.catalog.item_sha256 or "missing_reward_items")
            + (state_reader.template_sha256 or "missing_buff_templates")
        ).encode()
    ).hexdigest()[:16]
    output = directory / "derived" / "card_identity" / signature
    output.mkdir(parents=True, exist_ok=True)
    lock = output / "worker.lock"
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError(f"Analysis worker lock already exists: {lock}") from None
    os.close(descriptor)
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
    console_path = directory / "analysis_console.jsonl"

    def notice(message, **extra):
        with console_path.open("a", encoding="utf-8", buffering=1) as stream:
            stream.write(json.dumps({"message": message, "analysis_id": signature, **extra}, ensure_ascii=False) + "\n")

    def save_status():
        status["updated_at"] = datetime.now(timezone.utc).isoformat()
        temporary = status_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(status_path)

    action_console = OperationConsole()
    reward_console = RewardConsole()
    try:
        save_status()
        vision = OfflineVision(resource, output)
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
        notice("后台手牌与状态识别已就绪。手牌按从左到右排列：→ 选中，荐 推荐，禁 不可用，≈ 前缀匹配。")
        last_status_update = time.monotonic()
        cached = {}
        route_pending = {}
        last_summary = None
        last_event = time.monotonic()
        events_path = directory / "events.jsonl"
        while not events_path.exists():
            if time.monotonic() - last_event > 30:
                raise RuntimeError("Capture did not create events.jsonl")
            time.sleep(0.2)
        with events_path.open(encoding="utf-8") as source, (output / "index.jsonl").open("w", encoding="utf-8", buffering=1) as index:
            while True:
                position = source.tell()
                line = source.readline()
                if not line or not line.endswith("\n"):
                    source.seek(position)
                    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
                    writer_alive = time.time() - manifest.get("heartbeat_at", 0) < 15
                    if not follow or manifest["status"] != "recording" or (time.monotonic() - last_event > 120 and not writer_alive):
                        status["stop_reason"] = "snapshot_complete" if not follow else "capture_closed_or_idle"
                        break
                    if time.monotonic() - last_status_update >= 5:
                        save_status()
                        last_status_update = time.monotonic()
                    time.sleep(0.5)
                    continue
                last_event = time.monotonic()
                event = json.loads(line)
                if event["kind"].startswith("reward_") and event["kind"] != "reward_candidate_preview":
                    summary = reward_console.observe(event)
                    if summary:
                        notice(**summary)
                    continue
                if event["kind"] == "reward_candidate_preview":
                    result = {"error": "missing_frame", "detail_verified": False}
                    try:
                        frame_path = (directory / event["frame"]["path"]).resolve()
                        if not frame_path.is_relative_to(directory):
                            raise ValueError("Frame escaped capture directory")
                        array = normalized_image(read_image(frame_path))
                        result = reward_reader.read(array, event, vision._read_title)
                        result.update(source_frame_sha256=event["frame"]["sha256"], catalog_sha256=catalog.sha256)
                        semantic_status = result["effects"]["semantic_status"]
                        status["reward_effect_statuses"][semantic_status] = status["reward_effect_statuses"].get(semantic_status, 0) + 1
                        status["reward_preview_frames"] += 1
                    except Exception as exc:
                        result["error"] = str(exc)
                        status["reward_preview_errors"] += 1
                    result_path = output / f"reward_{event['seq']}.json"
                    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    index.write(json.dumps({
                        "event_id": event["event_id"], "session_id": event.get("session_id"),
                        "reward_page_id": event.get("reward_page_id"),
                        "candidate_id": event.get("candidate", {}).get("candidate_id"),
                        "result": str(result_path.relative_to(directory)).replace("\\", "/"),
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
                if event["kind"] in ("route_before", "route_after", "preparation_observation", "settlement_observation"):
                    result = {"error": "missing_frame", "candidates": []}
                    try:

                        frame_path = (directory / event["frame"]["path"]).resolve()
                        if not frame_path.is_relative_to(directory):
                            raise ValueError("Frame escaped capture directory")
                        array = normalized_image(read_image(frame_path))
                        tokens = vision._read_title(array, [0, 0, 720, 1280])
                        result = route_observation(tokens, catalog, event)
                        result.update(source_frame_sha256=event["frame"]["sha256"], catalog_sha256=catalog.sha256)
                        status["route_frames"] += 1
                    except Exception as exc:
                        result["error"] = str(exc)
                        status["route_errors"] += 1
                    result_path = output / f"route_{event['seq']}.json"
                    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    if event["kind"] == "route_before":
                        route_pending[event["event_id"]] = result
                    index.write(json.dumps({
                        "event_id": event["event_id"], "session_id": event.get("session_id"),
                        "decision_id": event.get("decision_id"),
                        "result": str(result_path.relative_to(directory)).replace("\\", "/"),
                        "game_action_confirmed": None,
                    }, ensure_ascii=False) + "\n")
                    status["event_links"] += 1
                    save_status()
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
                # Pre-action candidates are stronger evidence than re-detection; keep them event-specific.
                key = sha + ("_" + str(event["seq"]) if event.get("candidates") else "")
                result_path = output / f"{key}.json"
                if key not in cached:
                    try:
                        frame_path = (directory / event["frame"]["path"]).resolve()
                        if not frame_path.is_relative_to(directory):
                            raise ValueError("Frame escaped capture directory")
                        image = read_image(frame_path)
                        source_size = event["frame"].get("source_size", [image.shape[1], image.shape[0]])
                        candidates = (event.get("candidates") or None) if source_size == [720, 1280] else None
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
                    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    cached[key] = True
                    status["unique_frames_processed"] += 1
                else:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
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
                            "result": str(result_path.relative_to(directory)).replace("\\", "/"),
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
                if (output / "STOP").exists() or (max_frames and status["unique_frames_processed"] >= max_frames):
                    status["stop_reason"] = "stop_file_or_frame_limit"
                    break
        for message, event_id, number in action_console.unfinished():
            notice(message, decision_id=event_id, console_operation_number=number)
        for summary in reward_console.unfinished():
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
        notice(f"手牌分析停止：{exc}；采集与培育继续。", finished=True)
        raise
    finally:
        save_status()
        lock.unlink(missing_ok=True)
    return status


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-dir", type=Path, required=True)
    parser.add_argument("--resource", type=Path, required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--aliases", type=Path)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--max-frames", type=int)
    args = parser.parse_args()
    print(
        json.dumps(
            analyze_capture(args.capture_dir, args.resource, args.database, args.aliases, args.follow, args.max_frames),
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
