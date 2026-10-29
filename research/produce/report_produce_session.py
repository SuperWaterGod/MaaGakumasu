"""跨中断分段生成整次培育的证据报告。"""

import sys
import json
import hashlib
import argparse
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

# 内置 Python 不自动添加脚本目录，在工具入口统一设置仓库与 Agent 导入路径。
sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[2] / "agent")]

from produce.reporting import build_report, segment_directory
from produce.settlement import evaluation_check

from research.produce.produce_reporting import build_corpus


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_settlement(run: Path, resource: Path, vision=None):
    """只读取已保存的结束阶段画面，将结果写入独立版本目录。"""
    from utils.image_io import read_image
    from produce.analysis import OfflineVision, normalized_image
    from produce.settlement import PARAMETER_ICONS, FinalExamTracker, settlement_observation

    run = run.resolve()
    if read_json(run / "manifest.json").get("status") != "closed":
        raise ValueError("Settlement refresh requires a closed capture")
    event_bytes = (run / "events.jsonl").read_bytes()
    events = [json.loads(line) for line in event_bytes.decode("utf-8").splitlines()]
    bindings = read_json(run / "manifest.json").get("metadata", {}).get("bindings", {})
    exam_tracker = FinalExamTracker(bindings.get("scenario"))
    code_root = Path(__file__).resolve().parents[2] / "agent"
    provenance = {
        "events_sha256": hashlib.sha256(event_bytes).hexdigest(),
        "code_sha256": {name: hashlib.sha256((code_root / name).read_bytes()).hexdigest()
                        for name in ("produce/settlement.py", "produce/analysis.py", "utils/image_io.py")},
        "reader_cli_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "resource_ocr_sha256": {path.relative_to(resource).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                                for path in sorted((resource / "model/ocr").rglob("*")) if path.is_file()},
        "settlement_icons_sha256": {definition["template"]: hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
                                    for definition in PARAMETER_ICONS.values() for path in [resource / "image" / definition["template"]]},
    }
    from importlib.metadata import version

    provenance["maafw_version"] = version("maafw")
    signature = hashlib.sha256(json.dumps(provenance, sort_keys=True).encode()).hexdigest()[:16]
    output = run / "derived/settlement" / signature
    if (output / "status.json").exists():
        status = read_json(output / "status.json")
        if status.get("status") == "completed":
            return status, vision
        # 保留原生 OCR 中断时的输出，在新目录中重试。
        output = Path(tempfile.mkdtemp(prefix=signature + "_retry_", dir=output.parent))
    output.mkdir(parents=True, exist_ok=True)
    status = {"schema_version": 1, "status": "running", "analysis_id": signature, **provenance,
              "device_input_sent": False, "frames": 0, "field_observations": {key: 0 for key in ("score", "rank", "Vo", "Da", "Vi")}}
    status_path = output / "status.json"
    status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        if events and vision is None:
            vision = OfflineVision(resource)
        seen = {}
        with (output / "index.jsonl").open("w", encoding="utf-8") as stream:
            for event in events:
                exam_tracker.observe(event)
                if event["kind"] not in ("settlement_observation", "exam_result_observation"):
                    continue
                frame = event.get("frame") or {}
                frame_path = (run / frame.get("path", "")).resolve()
                if not frame_path.is_relative_to(run) or not frame_path.is_file():
                    raise ValueError("Missing settlement frame or path escaped capture")
                if hashlib.sha256(frame_path.read_bytes()).hexdigest() != frame.get("sha256"):
                    raise ValueError("Settlement frame hash mismatch")
                if frame["sha256"] not in seen:
                    array = normalized_image(read_image(frame_path))
                    tokens = vision._read_title(array, [0, 0, 720, 1280])
                    result = settlement_observation(tokens, lambda roi: vision._read_title(array, roi),
                                                    lambda definition: vision.match_buff_templates(array, definition))
                    exam_tracker.observe(event, result)
                    result["source_frame_sha256"] = frame["sha256"]
                    result_path = output / f"settlement_{event['seq']}.json"
                    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    seen[frame["sha256"]] = result_path
                    status["frames"] += 1
                    for key, reading in result["settlement"].items():
                        status["field_observations"][key] += reading["value"] is not None
                stream.write(json.dumps({"event_id": event["event_id"], "source_frame_sha256": frame["sha256"],
                                         "result": seen[frame["sha256"]].relative_to(run).as_posix()}) + "\n")
        status["status"] = "completed"
    except Exception as exc:
        status.update(status="failed", error=str(exc))
        raise
    finally:
        status_path.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return status, vision



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="debug/produce_sessions/<session_id>")
    parser.add_argument("--all", action="store_true", help="汇总目录下所有 session.json，生成单轮报告和多轮统计")
    parser.add_argument("--output", type=Path, help="派生报告输出目录；--all 时必填")
    parser.add_argument("--refresh-settlement", action="store_true", help="Re-OCR saved ending frames; never sends device input")
    parser.add_argument("--resource", type=Path, help="Resource bundle used for settlement OCR")
    args = parser.parse_args()
    if args.all:
        if not args.output:
            parser.error("--all requires --output")
        if args.refresh_settlement:
            parser.error("--all 不执行 OCR；如需补识别，请先对单个会话运行 --refresh-settlement")
        reports, failures = [], []
        for path in sorted(args.directory.glob("*/data/session.json")):
            try:
                reports.append(build_report(path.parent.parent, args.output / path.parent.parent.name, export_data=True))
            except (OSError, ValueError, KeyError, TypeError) as exc:
                failures.append({"session_path": str(path), "error": str(exc)})
        summary = build_corpus(reports, args.output, failures)
        print(json.dumps({"summary": summary["summary"], "failed_sessions": failures}, ensure_ascii=False))
        return
    if args.refresh_settlement:
        if not args.resource:
            parser.error("--refresh-settlement requires --resource")
        vision = None
        for segment in read_json(args.directory / "data/session.json")["segments"]:
            run = segment_directory(args.directory.resolve(), segment)
            _, vision = refresh_settlement(run, args.resource, vision)
    report = build_report(args.directory, args.output, export_data=True)
    print(
        json.dumps(
            {
                "session_id": report["session"]["session_id"],
                "complete": report["complete"],
                "routes": len(report["route_decisions"]),
                "gaps": len(report["gaps"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
