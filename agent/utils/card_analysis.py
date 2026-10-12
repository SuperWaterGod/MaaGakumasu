"""Separate, input-free MaaFramework worker for captured hand identity analysis."""

import os
import sys
import json
import math
import time
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone
from importlib.metadata import version

from PIL import Image

# Embedded release Python uses an isolated _pth and does not add the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from battle_state import STANCE_NAMES, STATE_FIELDS, ANOMALY_FIELDS, BattleStateReader, state_lines, state_changes
from skill_card_identity import SkillCardCatalog, normalize_name


def normalized_image(image):
    if abs(image.width / image.height - 720 / 1280) > 0.005:
        raise ValueError("Unsupported frame aspect ratio; will not rotate")
    return image.convert("RGB").resize((720, 1280), Image.Resampling.BILINEAR)


def card_name(identity):
    if identity.get("database_card_id"):
        name = identity.get("base_name", identity["database_name"].rstrip("+")) + "+" * identity.get("observed_plus_count", 0)
    else:
        name = identity.get("observed_name", "") or "牌名未识别"
    suffix = " [未知]" if identity["status"] == "unknown" else ""
    if identity.get("match_method") == "prefix":
        suffix = " ≈"
    return " ".join(name.split()) + suffix


def hand_summary(cards, selected_box=None):
    parts = []
    for index, card in enumerate(sorted(cards, key=lambda card: card["box"][0]), 1):
        label = {"cards": "", "suggestions": "荐 ", "useless": "禁 "}.get(card.get("label"), "? ")
        marker = "→" if card["box"] == selected_box else " "
        parts.append(f"{marker} {index}. {label}{card_name(card['identity'])}")
    return "\n".join(parts) if parts else "  未检测到手牌"


def after_console(event, before, result):
    state = result.get("battle_state")
    changes = state_changes(before, state)
    if not state or not state.get("hud_present"):
        return f"操作后 #{event['seq']}：当前画面未确认出牌 HUD", changes
    labels = {
        "turns_remaining": "剩余回合",
        "score": "分数",
        "stamina": "体力",
        "genki": "元气",
        "attribute": "属性",
        "attribute_multiplier_percent": "倍率%",
        "goodImpressionTurns": "好印象",
        "concentration": "集中",
        "motivation": "干劲",
    }
    labels.update({buff["key"]: buff["name"] for buff in state.get("buffs", [])})
    labels.update(
        {
            "anomaly_stance": "姿态",
            "anomaly_stance_stage": "姿态阶段",
            "anomaly_full_power_charge": "全力值",
            "anomaly_next_stance": "预告姿态",
            "anomaly_transition_after_turns": "距切换回合",
            "anomaly_held_cards_count": "保留牌数",
            "anomaly_held_cards_capacity": "保留容量",
        }
    )

    def display(value):
        return STANCE_NAMES.get(value, value) if isinstance(value, str) else value

    parts = [f"{labels.get(key, key)} {display(value['before'])}→{display(value['after'])}" for key, value in changes.items()]
    if not parts:
        return None, changes
    rows = ["｜".join(parts[index : index + 3]) for index in range(0, len(parts), 3)]
    decision = str(event.get("decision_id") or "?").rsplit(":", 1)[-1]
    return f"操作后 #{event['seq']}（关联 #{decision}）：\n" + "\n".join(rows), changes


def compact_state_lines(state):
    lines = state_lines(state)
    if not state or not state.get("hud_present"):
        return lines
    main = lines[0].removeprefix("状态：") + "｜" + lines[1].removeprefix("属性：")
    extra = [line for line in lines[2:] if not line.startswith("Buff：")]
    buffs = [line.removeprefix("Buff：") for line in lines[2:] if line.startswith("Buff：")]
    return [main, *extra, *(["Buff：" + "｜".join(buffs)] if buffs else [])]


def operation_console(number, before_event, before_result, after_event=None, after_result=None):
    before_result = before_result or {}
    after_result = after_result or {}
    before = before_result.get("battle_state")
    cards = before_result.get("cards", [])
    operation = before_event.get("operation")
    title = {"play_card": "出牌", "use_drink_task": "使用饮料", "skip_round": "跳过回合", "move_cards": "移动卡牌"}.get(operation, "操作后观察")
    selected = next((card for card in cards if card["box"] == before_event.get("selected_box")), None)
    if operation == "play_card":
        title += "：" + (card_name(selected["identity"]) if selected else "牌名未识别")
    title = f"操作 {number}｜{title}"
    started, ended = before_event.get("monotonic_ns"), (after_event or {}).get("monotonic_ns")
    if isinstance(started, int) and isinstance(ended, int) and ended >= started:
        title += f"｜{(ended - started) / 1e9:.1f}秒"
    lines = [title]
    reason = {
        "game_recommendation": "游戏推荐",
        "only_available_card": "仅一张可用",
        "timeout_detector_confidence": "超时兜底",
        "existing_use_all_drinks": "使用现有饮料",
        "no_available_cards": "无可用牌",
        "no_cards_ocr": "无手牌",
    }.get(before_event.get("reason"), before_event.get("reason"))
    if reason:
        lines.append("选择依据：" + reason)
    if before_result.get("error"):
        lines.append("操作前识别失败：" + before_result["error"])
    elif before_event:
        lines.extend(["操作前：" + compact_state_lines(before)[0], *compact_state_lines(before)[1:]])
    else:
        lines.append("操作前：缺少前帧，无法比较变化")
    if operation in ("play_card", "skip_round", "move_cards"):
        lines.extend(["手牌：", hand_summary(cards, before_event.get("selected_box"))])
    changes = {}
    if after_event is None:
        lines[0] += "｜后续观察缺失"
        lines.append("后续观察：未采集到操作后画面，无法比较变化")
    elif after_result.get("error"):
        lines.append("操作后识别失败：" + after_result["error"])
    else:
        message, changes = after_console(after_event, before, after_result)
        if not after_result.get("battle_state", {}).get("hud_present"):
            lines.append("后续观察：未确认出牌 HUD")
        elif not before or not before.get("hud_present"):
            lines.append("操作后：" + compact_state_lines(after_result.get("battle_state"))[0])
        elif message:
            lines.append("观察变化：" + "｜".join(message.splitlines()[1:]))
        else:
            lines.append("观察变化：前后可读数值未变化；不代表没有其他效果")
    trace = [f"前 #{before_event['seq']}"] if before_event else []
    if after_event:
        trace.append(f"后 #{after_event['seq']}")
    lines.append("采集索引：" + " → ".join(trace))
    return "\n".join(lines), changes


class OperationConsole:
    """Pair observations for display; raw capture IDs and action labels stay intact."""

    def __init__(self):
        self.count = 0
        self.pending = {}

    def start(self, event, result):
        self.count += 1
        self.pending[event["event_id"]] = (self.count, event, result)

    def finish(self, event, result):
        pending = self.pending.pop(event.get("decision_id"), None)
        if pending is None:
            self.count += 1
            pending = (self.count, {}, {})
        number, before_event, before_result = pending
        message, changes = operation_console(number, before_event, before_result, event, result)
        return message, changes, number

    def unfinished(self):
        pending, self.pending = self.pending, {}
        return [(operation_console(number, event, result)[0], event["event_id"], number) for number, event, result in pending.values()]


class OfflineVision:
    def __init__(self, resource_path, log_directory):
        import numpy as np
        from maa.define import LoggingLevelEnum
        from maa.tasker import Tasker
        from maa.toolkit import Toolkit
        from maa.resource import Resource
        from maa.controller import CustomController

        Toolkit.init_option(str(log_directory))
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
        import numpy as np
        from maa.pipeline import JRecognitionType, JNeuralNetworkDetect

        normalized = normalized_image(image)
        array = np.asarray(normalized)[:, :, ::-1].copy()
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
        result = {"analysis_size": [720, 1280], "source_size": list(image.size), "cards": cards}
        if getattr(self, "state_reader", None):
            try:
                result["battle_state"] = self.state_reader.read(array)
            except Exception as exc:
                result["battle_state"] = {"hud_present": False, "error": str(exc), "fields": {}, "buffs": []}
        return result


def analyze_capture(directory, resource, database, aliases=None, follow=False, max_frames=None):
    directory = directory.resolve()
    catalog = SkillCardCatalog(database, aliases)
    model_sha = hashlib.sha256((resource / "model/detect/cards.onnx").read_bytes()).hexdigest()
    state_module = Path(__file__).with_name("battle_state.py")
    state_templates = database.with_name("battle_buff_templates.json")
    state_reader = BattleStateReader(state_templates, None, image_root=resource / "image")
    signature = hashlib.sha256(
        (
            catalog.sha256
            + model_sha
            + version("maafw")
            + (hashlib.sha256(aliases.read_bytes()).hexdigest() if aliases and aliases.exists() else "")
            + Path(__file__).read_text(encoding="utf-8")
            + Path(__file__).with_name("skill_card_identity.py").read_text(encoding="utf-8")
            + state_module.read_text(encoding="utf-8")
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
                        with Image.open(frame_path) as image:
                            source_size = event["frame"].get("source_size", list(image.size))
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
