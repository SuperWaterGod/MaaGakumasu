"""单轮培育报告与同步收尾；命令入口不连接游戏设备。"""

import os
import sys
import json
import time
import uuid
import hashlib
from html import escape
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from produce.audit import enrich_report, analysis_records
from produce.replay import render_replay
from produce.settlement import FinalExamTracker, evaluation_check, ranking_observation


def read_json(path):
    """容忍 Windows 对正在原子替换的状态文件短暂拒绝访问。"""
    for attempt in range(6):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        for attempt in range(6):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build_report(session_directory: Path, output_directory: Path | None = None, *, export_data=False):
    directory = session_directory.resolve()
    session = read_json(directory / "data/session.json")
    if session.get("schema_version") != 4:
        raise ValueError("请使用当前版本生成的培育记录（schema_version=4）")
    capture_root = directory
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
    records = []
    seen_events = set()
    seen_runs = set()
    run_directories = {}
    nia_tracker = FinalExamTracker("NIA") if session["bindings"].get("scenario") == "NIA" else None
    for segment in session["segments"]:
        run_id = segment["run_id"]
        if run_id in seen_runs:
            gaps.append({"run_id": run_id, "reason": "duplicate_segment"})
            continue
        seen_runs.add(run_id)
        run = segment_directory(directory, segment)
        run_directories[run_id] = run
        if not (run / "events.jsonl").exists():
            gaps.append({"run_id": run_id, "reason": "missing_events"})
            continue
        try:
            manifest = read_json(run / "manifest.json")
        except (OSError, ValueError) as exc:
            gaps.append({"run_id": run_id, "reason": "invalid_manifest", "error": str(exc)})
            manifest = {}
        derived = {}
        selected_status = run / "analysis/status.json"
        status = None
        if selected_status.exists():
            try:
                status = read_json(selected_status)
            except (OSError, ValueError) as exc:
                gaps.append({"run_id": run_id, "reason": "invalid_analysis_status", "error": str(exc)})
        result_stream = run / "analysis/results.jsonl"
        if status and result_stream.exists():
            try:
                for link, result in analysis_records(result_stream):
                    derived[link["event_id"]] = {"link": link, "observation": result}
            except (ValueError, OSError, KeyError) as exc:
                gaps.append({"run_id": run_id, "reason": "invalid_analysis_record", "error": str(exc)})
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
        segments.append({"run_id": run_id, "manifest": manifest, "analysis": status,
                         "settlement_refresh": settlement_refresh})
        if nia_tracker:
            nia_tracker.pending = None
        for number, line in enumerate((run / "events.jsonl").read_text(encoding="utf-8").splitlines(), 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                gaps.append({"run_id": run_id, "reason": "invalid_event_json", "line": number})
                continue
            entry = {"event": event, "derived": derived.get(event["event_id"]),
                     "source": {"run_id": run_id, "events_path": str(run / "events.jsonl"), "line": number}}
            records.append(entry)
            if event.get("session_id") != session["session_id"] or event.get("run_id") != run_id:
                gaps.append({"event_id": event["event_id"], "reason": "session_id_mismatch"})
                continue
            if event["event_id"] in seen_events:
                gaps.append({"event_id": event["event_id"], "reason": "duplicate_event_id"})
                continue
            seen_events.add(event["event_id"])
            result_hash = ((entry["derived"] or {}).get("observation") or {}).get("source_frame_sha256")
            if result_hash and result_hash != event.get("frame", {}).get("sha256"):
                gaps.append({"event_id": event["event_id"], "reason": "analysis_frame_mismatch"})
                entry["derived"] = None
            counts[event["kind"]] += 1
            if nia_tracker:
                nia_tracker.observe(event)
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
                             "reward_selection_restored": "restore", "reward_selection_confirmed": "restore",
                             "reward_page_end": "end", "reward_receive_input": "receive",
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
                observation = (entry.get("derived") or {}).get("observation") or {}
                if nia_tracker:
                    # 使用当时保存的 OCR 重建旧报告，无需原图；已经确认的旧读数仍可沿用。
                    stored = observation.get("final_audition")
                    rebuilt = observation.copy()
                    if not rebuilt.get("exam_ranking"):
                        rebuilt["exam_ranking"] = ranking_observation(observation.get("raw_ocr") or [])
                    nia_tracker.observe(event, rebuilt)
                    if stored:
                        current = nia_tracker.final_audition or {}
                        if ((stored.get("score") or {}).get("value") is not None or current.get("outcome") != stored["outcome"]
                                or (current.get("score") or {}).get("value") is None):
                            nia_tracker.final_audition = stored
                        nia_tracker.pending = None
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
        page["identity_cache_count"] = sum(entry["event"].get("observation_method") in {"icon_cache", "upstream_icon"} for entry in page["previews"])
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
    final_audition = nia_tracker.final_audition if nia_tracker else None
    final_settlement = {}
    for field in ("score", "rank", "Vo", "Da", "Vi", "fan_votes", "exam_score", "place"):
        observations = []
        for entry in (*settlements, *exam_results):
            if field in ("Vo", "Da", "Vi") and entry["event"]["kind"] == "exam_result_observation":
                continue
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
    start_source = session["start_source"]
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
        "final_audition": final_audition,
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
        "coverage_scope": "pipeline_events; candidate_inventory_and_game_action_confirmation_remain_partial",
    }
    output = (output_directory or directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    report["report_path"] = str(output / "report.html")
    report["report_provenance"] = {"generated_at": datetime.now(timezone.utc).isoformat()}
    enrich_report(report, records, directory, capture_root)
    if export_data:
        atomic_json(output / "data/report.json", report)
    replay = render_replay(report)
    atomic_text(output / "report.html", "\n".join(replay))
    return report



def segment_directory(directory, segment):
    """只读取当前整局目录内的分段，不搜索旧外部布局。"""
    directory = Path(directory).resolve()
    run = (directory / segment["relative_path"]).resolve()
    if not run.is_relative_to(directory):
        raise ValueError("Segment escaped session directory")
    return run


def process_alive(pid):
    """保守检查后台是否仍存活，不向目标进程发送信号。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def notify_ready_reports(root, emit):
    """客户端进程转发完成消息；持久标记让关闭客户端后的报告在下次启动提醒。"""
    for path in Path(root).glob("*/data/report_status.json"):
        try:
            status = read_json(path)
            if status.get("notified") or status.get("status") not in ("completed", "failed", "cleanup_pending"):
                continue
            session = read_json(path.parent / "session.json")
            if session.get("schema_version") != 4 or session.get("status") == "recording":
                continue
            report = path.parent.parent / "report.html"
            if not report.exists() and (path.parent.parent / "report.md").exists():
                # 兼容升级前已完成但尚未通知的报告，不删除历史文件。
                report = path.parent.parent / "report.md"
            if report.exists():
                label = "培育回放已生成" if status["status"] == "completed" else "培育回放已保存，部分记录处理失败"
                emit(f'{label}：<a href="{escape(report.resolve().as_posix(), quote=True)}">打开培育回放</a>')
            else:
                emit("培育回放生成失败，请查看本局记录目录：" + str(path.parent))
            status["notified"] = True
            atomic_json(path, status)
        except (OSError, ValueError, KeyError):
            continue


def schedule_report(directory):
    """会话关闭后直接生成回放，完成即通知；保留失败标记供下次启动重试。"""
    directory = Path(directory).resolve()
    pending = directory / "data/report_pending.json"
    atomic_json(pending, {"requested_at": datetime.now(timezone.utc).isoformat()})
    try:
        build_report(directory)
        atomic_json(directory / "data/report_status.json", {"status": "completed"})
        pending.unlink(missing_ok=True)
    except Exception as exc:
        atomic_json(directory / "data/report_status.json", {"status": "failed", "error": str(exc)})
    from utils import logger
    notify_ready_reports(directory.parent, logger.info)


def recover_reports(root):
    """启动时一次性补生成已关闭会话的报告，不创建线程或轮询任务。"""
    for marker in Path(root).glob("*/data/report_pending.json"):
        try:
            session = read_json(marker.parent / "session.json")
            if session.get("schema_version") == 4 and session.get("status") != "recording":
                schedule_report(marker.parent.parent)
        except (OSError, ValueError, KeyError):
            continue
