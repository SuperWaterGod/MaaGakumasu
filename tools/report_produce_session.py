"""跨中断分段生成整次培育的证据报告。"""

import sys
import json
import hashlib
import argparse
import tempfile
from pathlib import Path
from datetime import datetime
from collections import Counter

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))

from produce.settlement import evaluation_check


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def refresh_settlement(run: Path, resource: Path, vision=None):
    """只读取已保存的结束阶段画面，将结果写入独立版本目录。"""
    from utils.image_io import read_image
    from produce.analysis import OfflineVision, normalized_image
    from produce.settlement import PARAMETER_ICONS, settlement_observation

    run = run.resolve()
    if read_json(run / "manifest.json").get("status") != "closed":
        raise ValueError("Settlement refresh requires a closed capture")
    event_bytes = (run / "events.jsonl").read_bytes()
    events = [event for line in event_bytes.decode("utf-8").splitlines()
              if (event := json.loads(line))["kind"] in ("settlement_observation", "exam_result_observation")]
    code_root = Path(__file__).resolve().parents[1] / "agent"
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
            vision = OfflineVision(resource, output)
        seen = {}
        with (output / "index.jsonl").open("w", encoding="utf-8") as stream:
            for event in events:
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


def build_report(session_directory: Path, capture_root: Path | None = None):
    directory = session_directory.resolve()
    session = read_json(directory / "session.json")
    capture_root = capture_root or directory.parent.parent / "decision_capture"
    counts = Counter()
    routes = {}
    reward_pages = {}
    battles = []
    settlements = []
    exam_results = []
    preparations = []
    observed_hand_cards = {}
    segments = []
    gaps = []
    for segment in session["segments"]:
        run_id = segment["run_id"]
        run = (capture_root / run_id).resolve()
        if not run.is_relative_to(capture_root.resolve()):
            raise ValueError("Segment escaped capture root")
        if not (run / "events.jsonl").exists():
            gaps.append({"run_id": run_id, "reason": "missing_events"})
            continue
        manifest = read_json(run / "manifest.json")
        derived = {}
        # 每个分段只使用一个分析版本，并在报告中记录所选版本。
        statuses = sorted((run / "derived/card_identity").glob("*/status.json"), key=lambda path: path.stat().st_mtime)
        status = read_json(statuses[-1]) if statuses else None
        if statuses and (statuses[-1].parent / "index.jsonl").exists():
            for line in (statuses[-1].parent / "index.jsonl").read_text(encoding="utf-8").splitlines():
                try:
                    link = json.loads(line)
                    result = None
                    if link.get("result"):
                        result_path = (run / link["result"]).resolve()
                        if not result_path.is_relative_to(run):
                            raise ValueError("Analysis result escaped segment")
                        result = read_json(result_path)
                    derived[link["event_id"]] = {"link": link, "observation": result}
                except (json.JSONDecodeError, OSError) as exc:
                    gaps.append({"run_id": run_id, "reason": "invalid_analysis_record", "error": str(exc)})
        effect_replay = None
        replays = sorted((run / "derived/reward_effects").glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for replay_path in replays:
            replay_status = read_json(replay_path)
            if replay_status.get("status") != "completed" or replay_status.get("source_analysis_id") != (status or {}).get("analysis_id"):
                continue
            if replay_status.get("events_sha256") != hashlib.sha256((run / "events.jsonl").read_bytes()).hexdigest():
                continue
            for line in (replay_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines():
                link = json.loads(line)
                original = derived.get(link["event_id"])
                if not original or original["link"].get("result") != link.get("source_result"):
                    raise ValueError("Reward effect replay source mismatch")
                result_path = (run / link["result"]).resolve()
                if not result_path.is_relative_to(run):
                    raise ValueError("Reward effect replay escaped segment")
                result = read_json(result_path)
                source_entry = result.get("replay", {}).get("source", {})
                source_path = (run / link["source_result"]).resolve()
                if not source_path.is_relative_to(run) or hashlib.sha256(source_path.read_bytes()).hexdigest() != source_entry.get("sha256"):
                    raise ValueError("Reward effect replay source changed")
                original["observation"] = result
                original["reward_effect_replay_result"] = link["result"]
            effect_replay = replay_status
            break
        settlement_refresh = None
        settlement_updates = {}
        refreshes = sorted((run / "derived/settlement").glob("*/status.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for refresh_path in refreshes:
            refresh_status = read_json(refresh_path)
            if refresh_status.get("status") != "completed":
                continue
            if refresh_status.get("events_sha256") != hashlib.sha256((run / "events.jsonl").read_bytes()).hexdigest():
                continue
            for line in (refresh_path.parent / "index.jsonl").read_text(encoding="utf-8").splitlines():
                link = json.loads(line)
                result_path = (run / link["result"]).resolve()
                if not result_path.is_relative_to(run):
                    raise ValueError("Settlement refresh escaped capture")
                result = read_json(result_path)
                if result.get("source_frame_sha256") != link.get("source_frame_sha256"):
                    raise ValueError("Settlement refresh frame mismatch")
                settlement_updates[link["event_id"]] = {"link": link, "observation": result}
            settlement_refresh = refresh_status
            break
        segments.append({"run_id": run_id, "manifest": manifest, "analysis": status, "reward_effect_replay": effect_replay,
                         "settlement_refresh": settlement_refresh})
        for number, line in enumerate((run / "events.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                gaps.append({"run_id": run_id, "reason": "invalid_event_json", "line": number})
                continue
            if event.get("session_id") != session["session_id"]:
                gaps.append({"event_id": event["event_id"], "reason": "session_id_mismatch"})
                continue
            counts[event["kind"]] += 1
            entry = {"event": event, "derived": derived.get(event["event_id"])}
            observation = (entry["derived"] or {}).get("observation") or {}
            if event["kind"] == "reward_page_start":
                reward_pages[event["event_id"]] = {
                    "start": entry, "previews": [], "inputs": [], "restore": None, "end": None, "receive": None,
                    "diagnostics": [], "retries": [],
                    "inventory_change_confirmed": None,
                }
            elif event["kind"].startswith("reward_") and event.get("reward_page_id"):
                page = reward_pages.get(event["reward_page_id"])
                if page:
                    field = {"reward_candidate_preview": "previews", "reward_preview_input": "inputs",
                             "reward_selection_restored": "restore", "reward_page_end": "end", "reward_receive_input": "receive",
                             "reward_preview_wait_diagnostic": "diagnostics", "reward_preview_retry": "retries"}.get(event["kind"])
                    if field in ("previews", "inputs", "diagnostics", "retries"):
                        page[field].append(entry)
                    elif field:
                        page[field] = entry
            if event["kind"] in ("battle_entry", "decision_before", "decision_after"):
                for card in observation.get("cards", []):
                    identity = card.get("identity", {})
                    card_id = identity.get("database_card_id")
                    if not card_id:
                        continue
                    seen = observed_hand_cards.setdefault(card_id, {"identity": identity, "events": []})
                    seen["events"].append(event["event_id"])
            if event["kind"] == "route_before":
                routes[event["event_id"]] = {"before": entry, "input": None, "after": None, "inventory_change_confirmed": None}
            elif event["kind"] in ("route_input_result", "route_after"):
                route = routes.get(event.get("decision_id"))
                if route is None:
                    gaps.append({"event_id": event["event_id"], "reason": "orphan_route_event"})
                else:
                    route["input" if event["kind"] == "route_input_result" else "after"] = entry
            elif event["kind"] == "battle_invocation_start":
                battles.append(event["event_id"])
            elif event["kind"] in ("settlement_observation", "exam_result_observation"):
                refreshed = settlement_updates.get(event["event_id"])
                if refreshed:
                    if refreshed["link"]["source_frame_sha256"] != event.get("frame", {}).get("sha256"):
                        raise ValueError("Settlement refresh event frame mismatch")
                    entry["derived"] = refreshed
                (settlements if event["kind"] == "settlement_observation" else exam_results).append(entry)
            elif event["kind"] == "preparation_observation":
                preparations.append(entry)
    for decision, route in routes.items():
        if route["input"] is None or route["after"] is None:
            gaps.append({"decision_id": decision, "reason": "missing_route_input_or_after"})
    for page_id, page in reward_pages.items():
        start = page["start"]["event"]
        for route_id in (start.get("decision_id"), start.get("confirmation_id")):
            if route_id in routes:
                routes[route_id].setdefault("reward_page_ids", []).append(page_id)
        expected = {candidate["candidate_id"] for candidate in start.get("layout", {}).get("candidates", [])}
        observed = {entry["event"].get("candidate", {}).get("candidate_id") for entry in page["previews"] if "frame" in entry["event"]}
        page["candidate_previews_complete"] = bool(expected and expected == observed and page["end"] and
                                                   page["end"]["event"].get("candidate_previews_complete"))
        page["details_verified_count"] = sum(bool(((entry.get("derived") or {}).get("observation") or {}).get("detail_verified"))
                                              for entry in page["previews"])
        page["identity_cache_count"] = sum(entry["event"].get("observation_method") == "icon_cache" for entry in page["previews"])
        page["identities_verified_count"] = sum(bool(((entry.get("derived") or {}).get("observation") or {}).get("identity_verified"))
                                                 for entry in page["previews"])
    missing_frames = []
    reward_effect_counts = Counter()
    for page in reward_pages.values():
        for preview in page["previews"]:
            observation = (preview.get("derived") or {}).get("observation") or {}
            reward_effect_counts[(observation.get("effects") or {}).get("semantic_status", "missing_analysis")] += 1
    for route in routes.values():
        for phase in ("before", "after"):
            entry = route[phase]
            if entry and "frame" not in entry["event"]:
                missing_frames.append(entry["event"]["event_id"])
    final_settlement = {}
    for field in ("score", "rank", "Vo", "Da", "Vi", "fan_votes", "exam_score", "place"):
        observations = []
        for entry in (*settlements, *exam_results):
            observation = (entry.get("derived") or {}).get("observation") or {}
            settlement = observation.get("settlement") or {}
            reading = (observation.get("fan_votes") or {}) if field == "fan_votes" else settlement.get(field, {})
            if field in ("exam_score", "place"):
                reading = (observation.get("final_exam") or {}).get("score" if field == "exam_score" else "place") or {}
            value = reading.get("value")
            if value is not None:
                observations.append({"event_id": entry["event"]["event_id"], **reading})
        values = {item["value"] for item in observations}
        final_settlement[field] = {
            "value": next(iter(values)) if len(values) == 1 else None,
            "observations": observations,
            "conflict": len(values) > 1,
        }
    final_fan_votes = final_settlement.pop("fan_votes")
    final_exam = {"score": final_settlement.pop("exam_score"), "place": final_settlement.pop("place")}
    formula_check = evaluation_check(session["bindings"]["scenario"], final_settlement, final_fan_votes, final_exam,
                                     session["bindings"].get("difficulty"))
    settlement_complete = all(item["value"] is not None for item in final_settlement.values())
    # 不把路线候选当作已持有牌组，也不把输入成功当作库存变更。
    gameplay_started_at = session.get("gameplay_started_at")
    started_at = gameplay_started_at or session["started_at"]
    start_source = session.get("start_source", "legacy_preparation_entry")
    if gameplay_started_at and started_at != session["started_at"]:
        start_source = "legacy_gameplay_entry"
    task_started_at = session.get("task_started_at")
    if not task_started_at and segments:
        task_started_at = (segments[0]["manifest"].get("metadata") or {}).get("task_started_at")
    report = {
        "schema_version": 1,
        "session": session,
        "segments": segments,
        "event_counts": dict(counts),
        "battles": battles,
        "route_decisions": routes,
        "reward_pages": reward_pages,
        "reward_effect_statuses": dict(reward_effect_counts),
        "preparation_observations": preparations,
        "settlement_observations": settlements,
        "final_settlement": final_settlement,
        "final_fan_votes": final_fan_votes,
        "final_exam": final_exam,
        "exam_result_observations": exam_results,
        "evaluation_check": formula_check,
        "settlement_fields_complete": settlement_complete,
        "unresolved_settlement_fields": [key for key, item in final_settlement.items() if item["value"] is None],
        "timing": {
            "started_at": started_at,
            "start_source": start_source,
            "recorded_session_started_at": session["started_at"],
            "task_started_at": task_started_at,
            "preparation_started_at": session.get("preparation_started_at"),
            "gameplay_started_at": gameplay_started_at,
            "ended_at": session.get("ended_at"),
            "wall_seconds": (datetime.fromisoformat(session["ended_at"]) - datetime.fromisoformat(started_at)).total_seconds()
            if session.get("ended_at")
            else None,
        },
        "outcome": {
            "end_reason": session.get("stop_reason"),
            "failure_stop_observed": session.get("failure_stop_observed", False),
            "success": False if session.get("failure_stop_observed") else None,
        },
        "deck": {
            "deck_id": session["bindings"]["deck"].get("deck_id"),
            "complete": False,
            "cards": None,
            "observed_hand_cards": list(observed_hand_cards.values()),
            "reason": "owned_inventory_not_confirmed; see visible preparation and battle evidence",
        },
        "gaps": gaps,
        "missing_route_frames": missing_frames,
        "complete": bool(
            session["start_observed"]
            and session["settlement_observed"]
            and session["status"] == "ended"
            and not gaps
            and not missing_frames
            and all(s["manifest"]["status"] == "closed" and s["manifest"].get("events_dropped", 0) == 0 for s in segments)
        ),
        "coverage_scope": "pipeline_events; candidate_inventory_and_game_action_confirmation_remain_partial",
    }
    (directory / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    bindings = session["bindings"]
    lines = [
        "# 培育会话采集报告",
        "",
        f"- 会话 ID：`{session['session_id']}`；状态：`{session['status']}`",
        f"- 会话起点：`{report['timing']['start_source']}`；开始：`{report['timing']['started_at']}`；跨度秒数：{report['timing']['wall_seconds']}",
        f"- 配置偶像/歌曲：`{json.dumps(bindings['configured_idol'], ensure_ascii=False)}`",
        f"- 剧本/难度：`{bindings['scenario']}` / `{bindings['difficulty']}`；偶像/玩家等级未确认",
        f"- 分段：{len(segments)}；出牌调用：{len(battles)}；路线节点操作：{len(routes)}",
        f"- 开始已观察：{session['start_observed']}；结算已观察：{session['settlement_observed']}；事件覆盖完整：{report['complete']}",
        f"- 关联缺口：{len(gaps)}；路线缺帧：{len(missing_frames)}",
        "",
        "配置身份与实际 OCR 身份分开保存。牌组仅有可见观测，尚未确认完整库存；路线候选不属于已持有牌。",
        "实际点击保存在 input；框架成功不等于购买、强化或删除成功。未知成本和结算字段保持 null。",
        "",
        "## 路线记录",
        "",
        "| 操作节点 | 候选牌名 | 选择输入 | 后续观察 |",
        "| --- | --- | --- |",
    ]
    for route in routes.values():
        before = route["before"]
        observation = (before.get("derived") or {}).get("observation") or {}
        names = "、".join(card["identity"].get("database_name", "未知") for card in observation.get("candidates", [])) or "未知/未解析"
        action = route["input"]["event"] if route["input"] else {}
        lines.append(
            f"| {before['event']['node']} | {names.replace('|', '/')} | {action.get('actual_point')} | {'有' if route['after'] else '缺失'} |"
        )
    if reward_pages:
        lines.extend(["", "## 奖励候选详情", "", "预览输入与恢复原选择分别记录；全部候选可见不等于当前效果已核对，也不等于已经获得。", ""])
        lines.append("- 效果来源状态：" + "；".join(f"{key}={value}" for key, value in reward_effect_counts.items()))
        lines.append("- 结构化效果来自数据库；异常详情只保存 OCR 原文和截图，待核对。旧报告中的正文解析保留为历史证据，"
                     "不代表当前效果、实际扣费或实际收益已确认。")
        for page_id, page in reward_pages.items():
            start = page["start"]["event"]
            names = []
            for preview in page["previews"]:
                raw = preview["event"]
                observation = (preview.get("derived") or {}).get("observation") or {}
                names.append(f"{raw['candidate']['candidate_id']}={observation.get('observed_name') or '未解析'}")
            lines.append(f"- `{page_id}`：{'；'.join(names)}；原选择 `{start.get('selected_candidate_id')}`；"
                         f"预览齐全 {page['candidate_previews_complete']}；详情核对 {page['details_verified_count']} 项；"
                         f"图标缓存 {page['identity_cache_count']} 项；"
                         f"恢复核对 {(page['restore'] or {}).get('event', {}).get('verified')}；"
                         f"诊断帧 {sum('frame' in entry['event'] for entry in page['diagnostics'])}；重试 {len(page['retries'])} 次。")
            failure = ((page['end'] or {}).get('event') or {}).get('failure_diagnostic') or {}
            if failure:
                lines.append(f"  原因：`{failure.get('candidate_id')}` / `{failure.get('reason')}`；诊断截图和分项证据见 report.json。")
    lines.extend(["", "## 结算观察", ""])
    lines.append("- 汇总：" + "；".join(f"{field}={entry['value']}" for field, entry in final_settlement.items()))
    lines.append(f"- 结算字段齐全：{settlement_complete}；事件覆盖完整不代表结算数值全部读出。粉丝投票数、考试分数不作为最终评价分数。")
    lines.append(f"- 最终粉丝投票数：{final_fan_votes['value']}；公式校验：{formula_check['status']}；计算值：{formula_check['calculated']}")
    if session["bindings"]["scenario"] == "初":
        lines.append(f"- 最终考试：{final_exam['score']['value']} 分；名次：{final_exam['place']['value']}；最终三维不重复增加名次奖励。")
    for entry in settlements:
        derived = (entry.get("derived") or {}).get("observation") or {}
        fields = derived.get("settlement") or {}
        lines.append(f"- `{entry['event']['event_id']}`：" + "；".join(f"{key}={value['value']}" for key, value in fields.items()))
    if not settlements:
        lines.append("- 未采集到结算画面。")
    lines.extend(["", "版本、文件 SHA-256、各分段清单、成本 OCR 证据和全部关联事件见 report.json。", ""])
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="debug/produce_sessions/<session_id>")
    parser.add_argument("--capture-root", type=Path)
    parser.add_argument("--refresh-settlement", action="store_true", help="Re-OCR saved ending frames; never sends device input")
    parser.add_argument("--resource", type=Path, help="Resource bundle used for settlement OCR")
    args = parser.parse_args()
    if args.refresh_settlement:
        if not args.resource:
            parser.error("--refresh-settlement requires --resource")
        capture_root = args.capture_root or args.directory.parent.parent / "decision_capture"
        vision = None
        for segment in read_json(args.directory / "session.json")["segments"]:
            run = (capture_root / segment["run_id"]).resolve()
            if not run.is_relative_to(capture_root.resolve()):
                raise ValueError("Segment escaped capture root")
            _, vision = refresh_settlement(run, args.resource, vision)
    report = build_report(args.directory, args.capture_root)
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
