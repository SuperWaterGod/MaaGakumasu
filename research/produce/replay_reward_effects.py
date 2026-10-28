"""使用已保存的奖励 OCR 重新查库，不发送输入，也不重新执行 OCR。"""

import sys
import json
import hashlib
import argparse
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[2] / "agent")]
from produce.audit import analysis_records
from utils.image_io import read_image
from produce.catalog import SkillCardCatalog
from produce.rewards import DETAIL_ROIS, RewardReader, reward_preview_observation


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def replay(directory: Path, database: Path):
    directory = directory.resolve()
    manifest = load(directory / "manifest.json")
    if manifest.get("status") != "closed":
        raise ValueError("Replay requires a closed capture")
    sources = sorted((directory / "analysis").glob("status.json"), key=lambda path: path.stat().st_mtime)
    if not sources:
        raise ValueError("No prior OCR analysis")
    source = sources[-1].parent
    status = load(source / "status.json")
    if status.get("status") != "completed":
        raise ValueError("Prior analysis is incomplete")
    catalog = SkillCardCatalog(database)
    if status.get("catalog_sha256") != catalog.sha256:
        raise ValueError("Replay catalog differs from original OCR analysis")
    events_path = directory / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    events = {event["event_id"]: event for event in events if event["kind"] == "reward_candidate_preview"}
    inputs = []
    for link, observation in analysis_records(source / "results.jsonl"):
        event = events.get(link["event_id"])
        if event is None:
            continue
        if observation.get("source_frame_sha256") != event.get("frame", {}).get("sha256"):
            raise ValueError("Source result frame mismatch")
        raw = json.dumps(observation, ensure_ascii=False, sort_keys=True).encode()
        inputs.append((event, observation, {"event_id": event["event_id"], "sha256": hashlib.sha256(raw).hexdigest()}))
    if not inputs or {event["event_id"] for event, _, _ in inputs} != set(events) or len(inputs) != len(events):
        raise ValueError("Reward OCR coverage is missing or duplicated")
    code_root = Path(reward_preview_observation.__code__.co_filename).parent
    hashes = {name: hashlib.sha256((code_root / name).read_bytes()).hexdigest()
              for name in ("catalog.py", "rewards.py")}
    hashes["utils/image_io.py"] = hashlib.sha256((code_root.parent / "utils/image_io.py").read_bytes()).hexdigest()
    items_path = database.with_name("produce_reward_items.json")
    items_sha = hashlib.sha256(items_path.read_bytes()).hexdigest() if items_path.exists() else None
    provenance = {"source_analysis_id": status["analysis_id"], "catalog_sha256": catalog.sha256,
                  "events_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest(), "code_sha256": hashes,
                  "reward_items_sha256": items_sha,
                  "inputs": [entry for _, _, entry in inputs]}
    signature = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()[:16]
    output = directory / "derived/reward_effects" / signature
    if output.exists():
        existing = load(output / "status.json")
        if existing.get("status") != "completed":
            raise ValueError("Prior replay output is incomplete")
        return existing
    output.mkdir(parents=True)
    reader = RewardReader(catalog, items_path, output)
    counts = Counter()
    reusable_icons = 0
    selected_ids = {event["event_id"]: event.get("selected_candidate_id") for event in
                    [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()] if event["kind"] == "reward_page_start"}
    with (output / "index.jsonl").open("x", encoding="utf-8") as stream:
        for event, previous, source_entry in inputs:
            if "raw_ocr_by_roi" not in previous:
                raise ValueError("Saved reward OCR is missing")
            frame_path = (directory / event["frame"]["path"]).resolve()
            if not frame_path.is_relative_to(directory):
                raise ValueError("Replay frame escaped capture")
            array = read_image(frame_path)
            # 使用已有 OCR 估计实时读取器需要请求的识别区域，
            # 不重新识别图像，也不改写历史输入事件。
            if (event["candidate"]["candidate_id"] != selected_ids.get(event.get("reward_page_id")) and
                    reader.icons and reader.icons.lookup(array, event["candidate"]["box"])):
                reusable_icons += 1

            def saved_ocr(_image, roi):
                key = next(name for name, region in DETAIL_ROIS.items() if list(region) == list(roi))
                return previous["raw_ocr_by_roi"].get(key, [])

            result = reader.read(array, event, saved_ocr)
            result.update(source_frame_sha256=previous["source_frame_sha256"], catalog_sha256=catalog.sha256,
                          replay={"method": "saved_ocr_only", "source_analysis_id": status["analysis_id"], "source": source_entry})
            counts[result["effects"]["semantic_status"]] += 1
            path = output / f"reward_{event['seq']}.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            stream.write(json.dumps({"event_id": event["event_id"], "result": path.relative_to(directory).as_posix(),
                                     "source_event_id": source_entry["event_id"]}, ensure_ascii=False) + "\n")
    result = {"schema_version": 2, "status": "completed", "analysis_id": signature, **provenance,
              "reward_observation_schema_version": 2, "method": "saved_ocr_only", "device_input_sent": False, "fresh_ocr": False,
              "reward_candidates": len(inputs), "semantic_statuses": dict(counts),
              "reward_catalog_hashes": reader.catalog.hashes, "estimated_roi_requests": reader.counts,
              "estimated_reusable_nonselected_icons": reusable_icons,
              "output": str(output), "updated_at": datetime.now(timezone.utc).isoformat()}
    (output / "status.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Closed debug/decision_capture/<run_id>")
    parser.add_argument("--database", type=Path, required=True)
    args = parser.parse_args()
    status = replay(args.directory, args.database)
    print(json.dumps({key: status[key] for key in ("analysis_id", "reward_candidates", "semantic_statuses", "output")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
