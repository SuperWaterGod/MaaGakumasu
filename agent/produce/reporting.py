"""单轮培育报告与同步收尾；命令入口不连接游戏设备。"""

import os
import sys
import json
import time
import uuid
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from produce.audit import enrich_report, analysis_records
from produce.settlement import evaluation_check


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
    report["report_path"] = str(output / "report.md")
    report["report_provenance"] = {"generated_at": datetime.now(timezone.utc).isoformat()}
    enrich_report(report, records, directory, capture_root)
    if export_data:
        atomic_json(output / "data/report.json", report)
    replay = render_replay(report)
    atomic_text(output / "report.md", "\n".join(replay))
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


def render_replay(report):
    """用户回放只展示游戏过程；原始参数、审计与版本信息留在结构化数据中。"""
    from produce.audit import cell
    session = report["session"]
    bindings = session["bindings"]
    configured = bindings.get("configured_idol") or {}
    zone = datetime.fromisoformat(session.get("local_started_at") or session["started_at"]).tzinfo

    def clock(value):
        try:
            return datetime.fromisoformat(value).astimezone(zone).strftime("%H:%M:%S")
        except (ValueError, TypeError):
            return "时间未知"

    def show(value):
        return "未知" if value is None else str(value)

    labels = {"score": "分数", "rank": "评级", "stamina": "体力", "genki": "元气", "turns_remaining": "剩余回合",
              "attribute": "属性", "attribute_multiplier_percent": "属性倍率", "health": "体力", "points": "P 点",
              "Vo": "声乐", "Da": "舞蹈", "Vi": "形象", "vote": "票数"}
    reason_names = {"game_recommendation": "游戏推荐", "only_available_card": "仅一张可用牌",
                    "timeout_detector_confidence": "等待推荐超时，使用现有兜底选择", "no_available_cards": "没有可用牌",
                    "existing_use_all_drinks": "使用现有饮料", "no_cards_ocr": "未发现手牌"}
    action_names = {"play_card": "出牌", "use_drink_task": "使用饮料", "skip_round": "跳过回合", "move_cards": "移动卡牌"}
    node_names = {"ProduceShoppingBuy": "点击购买", "ProduceShoppingBuySale": "点击折扣商品", "ProduceShoppingDecide": "确认购物",
                  "ProduceShoppingExit": "离开商店", "ProduceChooseStrengthenFlag": "进入强化并选择对象",
                  "ProduceChooseDeleteFlag": "进入删除、复制或变换并选择对象", "ProduceChooseStrengthen": "确认强化", "ProduceChooseStrengthenClick": "选择强化对象",
                  "ProduceChooseDeleteClick": "选择删除、复制或变换对象", "ProduceChooseConfirm": "确认选择",
                  "ProduceGeneration": "点击生成", "ProduceFinished": "完成结算"}
    status = "已结束" if session["status"] == "ended" else "未完成"
    if session.get("failure_stop_observed"):
        status = "失败终止"
    difficulty = bindings.get("difficulty") or "未知"
    if isinstance(difficulty, list):
        difficulty = "/".join(difficulty)
    lines = ["# 培育回放", "",
             f"- 配置偶像：{configured.get('idol_name') or '未知'}；歌曲：{configured.get('song_name') or '未知'}",
             f"- 剧本：{bindings.get('scenario', '未知')} / {difficulty}；状态：{status}",
             f"- 开始：{clock(session['started_at'])}；结束：{clock(session.get('ended_at'))}",
             "", "## 结算", "",
             "；".join(f"{labels.get(key, key)}：{show(item.get('value'))}" for key, item in report['final_settlement'].items()),
             "", "## 过程", "",
             "时间按本局时区显示。“选择前观测”是与上次读取的比较，不是本次操作的效果；战斗变化来自操作后的观测。", "",
             ]
    timeline = report["timeline"]
    after = {row["detail"].get("decision_id"): row for row in timeline if row["kind"] in ("decision_after", "produce_event_input_result", "route_input_result")}
    reward_names = {}
    for row in timeline:
        if row["kind"] == "reward_candidate_preview":
            detail = row["detail"]
            reward_names[(detail.get("reward_page_id"), (detail.get("candidate") or {}).get("candidate_id"))] = detail.get("observed_name")
    last_values = {}

    def observations(detail):
        parts = []
        def visit(key, value):
            if isinstance(value, dict) and "value" in value:
                value = value["value"]
            if value is None:
                return
            if isinstance(value, dict) and "current" in value:
                value = f"{show(value['current'])}/{show(value.get('max'))}"
            if isinstance(value, dict):
                for child, item in value.items():
                    visit(child, item)
                return
            if key not in labels:
                return
            previous = last_values.get(key)
            if previous is None:
                parts.append(f"{labels[key]} {value}")
            elif previous != value:
                parts.append(f"{labels[key]} {previous} → {value}")
            last_values[key] = value
        for key, value in (detail.get("observations") or {}).items():
            visit(key, value)
        return "；".join(parts)

    battle_rows = []
    battle_pictures = []
    battle_key = None
    battle_number = 0

    def append_pictures(pictures):
        shown = set()
        images = []
        for caption, picture in pictures:
            path = (picture or {}).get("evidence", {}).get("frame_path")
            if path and path not in shown and Path(path).is_file():
                shown.add(path)
                relative = Path(os.path.relpath(path, Path(report["report_path"]).parent)).as_posix()
                images.extend([f"![{caption}](<{relative}>)", ""])
        if images:
            lines.extend(["", "<details>", "<summary>查看截图</summary>", "", *images, "</details>", ""])

    def flush_battle():
        nonlocal battle_key, battle_number
        if not battle_rows:
            return
        battle_number += 1
        lines.extend(["", f"### 战斗 {battle_number} · {battle_rows[0][1]}—{battle_rows[-1][1]}", "",
                      "每行是一次操作；范围为已记录的操作时间，不能据此认定考试成功。", "",
                      "| 操作 | 时间 | 当时手牌 / 选项 | 实际选择与依据 | 观察变化 |", "| --- | --- | --- | --- | --- |"])
        for values in battle_rows:
            lines.append("| " + " | ".join(cell(value) for value in values) + " |")
        append_pictures(battle_pictures)
        battle_rows.clear()
        battle_pictures.clear()
        battle_key = None

    for row in timeline:
        kind, detail = row["kind"], row["detail"]
        if kind in ("battle_invocation_start", "battle_invocation_end", "battle_exit"):
            flush_battle()
            continue
        event = operation = ""
        changes = observations(detail)
        if changes:
            changes = "选择前观测：" + changes
        if kind == "produce_event_choice":
            event = "路线选择"
            chosen = (detail.get("selected") or {}).get("name") or "未识别选项"
            operation = "选择 " + chosen.replace("_SP", " SP 课程")
            result = after.get(row["evidence"]["event_id"])
            if result and result["detail"].get("framework_action_succeeded") is False:
                operation += "（执行失败）"
        elif kind == "option_choice":
            event = detail.get("title") or "选项选择"
            operation = (detail.get("action") or "发送选择输入").replace("null", "无属性选项")
        elif kind == "decision_before":
            event = "战斗"
            operation = action_names.get(detail.get("operation"), "战斗操作")
            if detail.get("operation") == "play_card":
                card = next((card.get("name") for card in detail.get("cards", []) if card.get("box") == detail.get("selected_box")), None)
                operation += "：" + (card or "牌名未识别")
            reason = reason_names.get(detail.get("reason"))
            if reason:
                operation += f"（{reason}）"
            following = after.get(row["evidence"]["event_id"])
            if following:
                parts = []
                for key, change in (following["detail"].get("state_changes") or {}).items():
                    if key in labels and isinstance(change, dict):
                        parts.append(f"{labels[key]} {show(change.get('before'))} → {show(change.get('after'))}")
                changes = "；".join(parts)
        elif kind == "reward_receive_input":
            event = "奖励选择"
            name = reward_names.get((detail.get("reward_page_id"), detail.get("selected_candidate_id")))
            operation = "点击领取：" + (name or "奖励名称未识别")
            if detail.get("framework_action_succeeded") is False:
                operation += "（点击失败）"
        elif kind == "route_before" and detail.get("node") in node_names and detail.get("node") != "ProduceChooseConfirm":
            node = detail["node"]
            event = "卡牌调整" if "ChooseDelete" in node else "强化" if "Strengthen" in node else "商店"
            operation = node_names[node]
            following = after.get(row["evidence"]["event_id"])
            if not following:
                operation += "（执行结果未记录）"
            elif following["detail"].get("framework_action_succeeded") is False:
                operation += "（点击失败）"
            changes = "实际效果未确认"
        elif kind == "pipeline_action" and detail.get("node") in node_names:
            event, operation = "流程操作", node_names[detail["node"]]
        elif kind == "pipeline_failure":
            event, operation = "流程异常", "识别或执行未成功"
        elif kind == "exam_failure_or_retry":
            event, operation = "考试", "检测到失败或重试流程"
        elif kind in ("produce_session_start", "produce_session_resume", "produce_session_end"):
            event = "培育"
            operation = {"produce_session_start": "开始记录", "produce_session_resume": "继续中断的培育",
                         "produce_session_end": "结束本段记录"}[kind]
        elif kind in ("settlement_observation", "exam_result_observation"):
            values = detail.get("settlement") or {}
            if not any(value is not None for value in values.values()):
                continue
            event, operation = "结算" if kind == "settlement_observation" else "考试结果", "读取页面结果"
            changes = "；".join(f"{labels[key]} {show(value)}" for key, value in values.items() if key in labels and value is not None)
        else:
            continue
        if kind == "decision_before":
            key = (row["evidence"]["run_id"], detail.get("battle_id") or row["evidence"]["event_id"])
            if battle_key is not None and key != battle_key:
                flush_battle()
            battle_key = key
            number = len(battle_rows) + 1
            cards = "；".join(f"{index}. {card.get('name') or '未识别'}"
                              + ("（已选择）" if card.get("box") == detail.get("selected_box") else "")
                              for index, card in enumerate(detail.get("cards") or [], 1))
            battle_rows.append([number, clock(row["evidence"]["time"]), cards or "未识别", operation, changes or "未确认"])
            battle_pictures.append((f"操作 {number} 前", row))
            continue
        flush_battle()
        lines.extend(["", f"### {clock(row['evidence']['time'])} · {cell(event)}", "",
                      "| 项目 | 内容 |", "| --- | --- |", f"| 实际操作 | {cell(operation)} |"])
        if kind in ("produce_event_choice", "option_choice"):
            candidates = []
            for candidate in detail.get("candidates") or []:
                if isinstance(candidate, dict):
                    candidates.extend(str(name).replace("null", "无属性选项") + (" SP" if candidate.get("SP") else "")
                                      for name in candidate if name != "SP")
            lines.append(f"| 可选项 | {cell('、'.join(candidates) or '未记录')} |")
        elif kind == "reward_receive_input":
            candidates = [name or "未识别" for (page, _), name in reward_names.items() if page == detail.get("reward_page_id")]
            lines.append(f"| 奖励候选 | {cell('、'.join(candidates) or '未识别')} |")
        if changes or event not in {"奖励选择", "工作选择", "属性选择"}:
            lines.append(f"| 观察变化 | {cell(changes or '未确认')} |")
        pictures = [("操作前", row)]
        if kind == "reward_receive_input":
            page = report.get("reward_pages", {}).get(detail.get("reward_page_id"), {})
            start = (page.get("start") or {}).get("event") or {}
            start_row = next((item for item in timeline if item["evidence"]["event_id"] == start.get("event_id")), None)
            pictures = [("奖励页面", start_row)]
        append_pictures(pictures)

    flush_battle()
    warnings = []
    if not report["settlement_fields_complete"]:
        warnings.append("结算信息未全部识别，未知项已保留。")
    if report.get("gaps") or report.get("integrity", {}).get("errors"):
        warnings.append("本局部分记录或识别结果缺失，以上仅展示已记录内容，不能视为完整过程。")
    if warnings:
        lines.extend(["", "## 记录说明", "", *["- " + warning for warning in warnings]])
    return [*lines, ""]


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
            report = path.parent.parent / "report.md"
            if report.exists():
                label = "培育回放已生成" if status["status"] == "completed" else "培育回放已保存，部分记录处理失败"
                from html import escape
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
