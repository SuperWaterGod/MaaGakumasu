"""Separate, input-free MaaFramework worker for captured hand identity analysis."""

import os
import sys
import json
import time
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone
from importlib.metadata import version

from PIL import Image

# Embedded release Python uses an isolated _pth and does not add the script directory.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from skill_card_identity import SkillCardCatalog, normalize_name


def normalized_image(image):
    if abs(image.width / image.height - 720 / 1280) > 0.005:
        raise ValueError("Unsupported frame aspect ratio; will not rotate")
    return image.convert("RGB").resize((720, 1280), Image.Resampling.BILINEAR)


def hand_summary(cards):
    parts = []
    for index, card in enumerate(cards, 1):
        identity = card["identity"]
        name = " ".join(identity.get("recognized_name", identity.get("observed_name", "")).split()) or "牌名未识别"
        label = {"cards": "普通", "suggestions": "推荐", "useless": "不可用"}.get(card.get("label"), "状态未知")
        match = {"matched": "已匹配", "base_match_variant_unresolved": "牌名已匹配，强化待确认"}.get(identity["status"], "未匹配")
        if identity.get("match_method") == "prefix":
            match = "前缀匹配，强化待确认"
        parts.append(f"{index}. {name}（{label}，{match}）")
    return "；".join(parts) if parts else "未检测到手牌"


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
        return {"analysis_size": [720, 1280], "source_size": list(image.size), "cards": cards}


def analyze_capture(directory, resource, database, aliases=None, follow=False, max_frames=None):
    directory = directory.resolve()
    catalog = SkillCardCatalog(database, aliases)
    model_sha = hashlib.sha256((resource / "model/detect/cards.onnx").read_bytes()).hexdigest()
    signature = hashlib.sha256(
        (
            catalog.sha256
            + model_sha
            + version("maafw")
            + (hashlib.sha256(aliases.read_bytes()).hexdigest() if aliases and aliases.exists() else "")
            + Path(__file__).read_text(encoding="utf-8")
            + Path(__file__).with_name("skill_card_identity.py").read_text(encoding="utf-8")
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

    try:
        save_status()
        vision = OfflineVision(resource, output)
        from maa.library import Library

        status["framework_version"] = Library.version()
        status["status"] = "processing"
        save_status()
        notice("后台卡牌识别已就绪，等待出牌画面。")
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
                if "frame" not in event or event["kind"] not in ("decision_before", "decision_after", "screen_observation"):
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
                    except Exception as exc:
                        result = {"schema_version": 1, "error": str(exc), "cards": [], "source_frame_sha256": sha}
                        status["errors"] += 1
                    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    cached[key] = True
                    status["unique_frames_processed"] += 1
                else:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                summary = f"本帧识别失败：{result['error']}" if result.get("error") else hand_summary(result["cards"])
                phase = {"decision_before": "操作前", "decision_after": "操作后", "screen_observation": "旁路"}[event["kind"]]
                if summary != last_summary:
                    notice(f"手牌识别（{phase} #{event['seq']}）：{summary}", event_id=event["event_id"])
                    last_summary = summary
                if event["kind"] == "decision_before" and event.get("selected_box"):
                    selected = [card for card in result["cards"] if card["box"] == event["selected_box"]]
                    if selected:
                        notice(f"出牌选择记录（#{event['seq']}）：{hand_summary(selected)}", event_id=event["event_id"])
                index.write(
                    json.dumps(
                        {
                            "event_id": event["event_id"],
                            "frame_sha256": sha,
                            "result": str(result_path.relative_to(directory)).replace("\\", "/"),
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
        status["status"] = "completed"
        notice(
            f"手牌分析结束：匹配 {status['matched']}，牌名已匹配但强化待确认 {status['variant_unresolved']}，未知 {status['unknown']}。", finished=True
        )
    except Exception as exc:
        status.update(status="failed", error=str(exc))
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
