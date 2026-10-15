"""培育实验的离线审计、过程串联和跨轮统计，不参与客户端运行。"""

import json
import hashlib
from pathlib import Path
from datetime import datetime
from collections import Counter, defaultdict


def timestamp(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def metric(success, total):
    return {"success": success, "total": total, "rate": success / total if total else None}


def observation(entry):
    return ((entry.get("derived") or {}).get("observation") or {})


def source(entry, capture_root):
    """原始 JSONL 是可精确定位的过程日志，截图和派生结果另附路径。"""
    raw = entry["event"]
    location = entry["source"]
    run = (capture_root / location["run_id"]).resolve()
    frame = raw.get("frame") or {}
    derived = (entry.get("derived") or {})
    result = derived.get("reward_effect_replay_result") or derived.get("link", {}).get("result")
    return {**location, "event_id": raw["event_id"], "time": raw.get("observed_at") or raw.get("time"),
            "frame_path": str((run / frame["path"]).resolve()) if frame.get("path") else None,
            "frame_sha256": frame.get("sha256"), "analysis_path": str((run / result).resolve()) if result else None}


def enrich_report(report, records, directory, capture_root):
    """保留原报告字段，以独立证据审计取代仅依赖会话布尔值的完整性判断。"""
    capture_root = capture_root.resolve()
    session = report["session"]
    issues = []
    by_id = {}
    valid = []
    frame_cache = {}
    segment_events = defaultdict(list)

    def issue(code, message, entries=(), severity="error", **extra):
        evidence = []
        for entry in entries:
            evidence.append(source(entry, capture_root))
            if not entry["event"].get("frame"):
                parent = by_id.get(entry["event"].get("decision_id") or entry["event"].get("reward_page_id"))
                if parent:
                    evidence.append(source(parent, capture_root))
        issues.append({"code": code, "message": message, "severity": severity, "evidence": evidence,
                       "session_path": str(directory / "session.json"), **extra})

    for entry in records:
        event = entry["event"]
        run_id = entry["source"]["run_id"]
        if event.get("session_id") != session["session_id"] or event.get("run_id") != run_id:
            issue("foreign_event", "事件会话或分段身份不符；已排除该记录。", [entry])
            continue
        if event["event_id"] in by_id:
            issue("duplicate_event", "同一事件 ID 重复；仅使用首次记录。", [by_id[event["event_id"]], entry])
            continue
        by_id[event["event_id"]] = entry
        valid.append(entry)
        segment_events[run_id].append(entry)
        frame = event.get("frame") or {}
        result_hash = observation(entry).get("source_frame_sha256")
        if result_hash and result_hash != frame.get("sha256"):
            issue("analysis_frame_mismatch", "派生结果对应截图与原事件不同；已排除派生值。", [entry])
            entry["derived"] = None
        if frame.get("path"):
            run = (capture_root / run_id).resolve()
            path = (run / frame["path"]).resolve()
            key = (str(path), frame.get("sha256"))
            if key not in frame_cache:
                if not path.is_relative_to(run):
                    frame_cache[key] = "截图路径越出分段目录"
                elif not path.is_file():
                    frame_cache[key] = "截图文件缺失"
                elif not frame.get("sha256"):
                    frame_cache[key] = "截图缺少 SHA-256，无法验证"
                else:
                    frame_cache[key] = None if hashlib.sha256(path.read_bytes()).hexdigest() == frame["sha256"] else "截图 SHA-256 不符"
            if frame_cache[key]:
                issue("frame_integrity", frame_cache[key], [entry])
        elif event["kind"] in ("produce_event_choice", "route_before", "route_after", "reward_candidate_preview",
                                "settlement_observation", "exam_result_observation"):
            issue("missing_frame", "该观察没有保存截图。", [entry])

    for gap in report["gaps"]:
        linked = by_id.get(gap.get("event_id") or gap.get("decision_id"))
        issue(gap["reason"], "原报告关联缺口：" + gap["reason"], [linked] if linked else [], detail=gap,
              events_path=str(capture_root / gap.get("run_id", "") / "events.jsonl"))

    versions = set()
    active_seconds = 0.0
    duration_known = True
    previous_end = None
    previous_terminal = False
    boundaries = []
    for index, segment in enumerate(report["segments"]):
        run_id = segment["run_id"]
        manifest = segment["manifest"]
        metadata = manifest.get("metadata") or {}
        entries = segment_events[run_id]
        hashes = metadata.get("version_files")
        version_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()[:16] if hashes else "未记录"
        versions.add(version_id)
        segment["code_version"] = version_id
        if manifest.get("run_id") != run_id or metadata.get("session_id") != session["session_id"]:
            issue("manifest_identity", "分段清单身份与会话不符。", entries[:1], run_id=run_id)
        bindings = metadata.get("bindings") or {}
        if bindings and any(bindings.get(key) != session["bindings"].get(key) for key in ("scenario", "difficulty", "configured_idol")):
            issue("configuration_changed", "分段配置与会话不一致，存在串轮风险。", entries[:1], run_id=run_id)
        if manifest.get("status") != "closed" or manifest.get("events_dropped", 0):
            issue("capture_incomplete", "采集未正常关闭或存在丢弃事件。", entries[-1:], run_id=run_id,
                  status=manifest.get("status"), events_dropped=manifest.get("events_dropped"))
        if manifest.get("events_written") is not None and manifest["events_written"] != len(entries):
            issue("event_count_mismatch", "清单记录数与有效事件行数不一致。", entries[-1:], run_id=run_id)
        seqs = [entry["event"].get("seq") for entry in entries]
        if seqs and seqs != list(range(1, len(seqs) + 1)):
            issue("sequence_gap", "事件序号跳跃或乱序，可能有丢弃记录。", entries[:1], run_id=run_id)
        times = [timestamp(entry["event"].get("time")) for entry in entries]
        if any(a is not None and b is not None and b < a for a, b in zip(times, times[1:])):
            issue("clock_reversed", "分段事件时间倒退，时间排序与耗时需谨慎。", entries[:1], run_id=run_id)
            duration_known = False
        starts = [e for e in entries if e["event"]["kind"] in ("produce_session_start", "produce_session_resume")]
        ends = [e for e in entries if e["event"]["kind"] == "produce_session_end"]
        gameplay = [e for e in entries if e["event"]["kind"] == "produce_gameplay_entry"]
        if len(starts) != 1 or len(ends) != 1:
            issue("segment_boundary_missing", "分段起止事件不齐或重复。", starts + ends, run_id=run_id)
        if ends and any(e["source"]["line"] > ends[-1]["source"]["line"] and e["event"]["kind"] != "analysis_started" for e in entries):
            issue("events_after_segment_end", "分段结束后仍有过程事件，需核查边界或串轮。", ends, run_id=run_id)
        if sum(bool(e["event"].get("fresh_start")) for e in gameplay) > 1:
            issue("multiple_fresh_starts", "同一分段记录了多次新开局，存在串轮风险。", gameplay)
        if index and (not starts or starts[0]["event"]["kind"] != "produce_session_resume" or previous_terminal):
            issue("invalid_resume", "续段缺少续跑标记，或上一段已经终止，存在串轮风险。", starts, run_id=run_id)
        if index and any(e["event"].get("fresh_start") for e in gameplay):
            issue("fresh_start_in_resume", "续段记录到新游戏开始，疑似串轮。", gameplay)
        if starts and starts[0]["event"].get("bindings"):
            event_bindings = starts[0]["event"]["bindings"]
            if any(event_bindings.get(k) != session["bindings"].get(k) for k in ("scenario", "difficulty", "configured_idol")):
                issue("start_configuration_changed", "分段起点配置与会话不一致。", starts)
        start = timestamp(manifest.get("started_at"))
        end = timestamp(manifest.get("ended_at"))
        if previous_end is not None and start is not None and start < previous_end:
            issue("overlapping_segments", "相邻分段时间重叠，存在串轮风险。", starts, run_id=run_id)
        boundaries.append({"run_id": run_id, "entry": metadata.get("entry"),
                           "continuity": starts[0]["event"].get("continuity") if starts else None,
                           "start": manifest.get("started_at"), "end": manifest.get("ended_at"),
                           "pause_before_seconds": start - previous_end if start is not None and previous_end is not None else None})
        # 第一段从游戏循环起点计时；续段只累计实际记录区间，排除中断等待。
        if not index:
            start = timestamp(session.get("gameplay_started_at"))
        if start is None or end is None or end < start:
            duration_known = False
        else:
            active_seconds += end - start
        previous_end = end
        previous_terminal = bool(ends and ends[-1]["event"].get("terminal"))
        analysis = segment.get("analysis")
        if not analysis or analysis.get("status") != "completed":
            issue("analysis_incomplete", "缺少已完成的离线分析；识别缺失不能计作零。", entries[:1], severity="warning", run_id=run_id)

    choices = {}
    for entry in valid:
        event = entry["event"]
        kind = event["kind"]
        if kind == "produce_event_choice":
            choices[event["event_id"]] = {"choice": entry, "input": None}
        elif kind == "produce_event_input_result":
            choice = choices.get(event.get("decision_id"))
            if choice:
                if choice["input"]:
                    issue("duplicate_event_input", "同一培育事件记录了多个执行结果。", [choice["input"], entry])
                choice["input"] = entry
            else:
                issue("orphan_event_input", "培育事件输入找不到选择记录。", [entry])
        if kind.startswith("reward_") and event.get("reward_page_id") not in (None, *report["reward_pages"]):
            issue("orphan_reward_event", "奖励记录找不到所属页面。", [entry])
    for choice in choices.values():
        if not choice["input"]:
            issue("missing_event_input", "培育事件缺少执行结果。", [choice["choice"]])

    confirmations = {}
    previous_page = None
    receives = defaultdict(list)
    for entry in valid:
        if entry["event"]["kind"] == "reward_receive_input":
            receives[entry["event"].get("reward_page_id")].append(entry)
    candidate_total = candidate_known = candidate_expected = 0
    for page_id, page in report["reward_pages"].items():
        start = page["start"]["event"]
        confirmation = start.get("confirmation_id")
        if confirmation and confirmation in confirmations:
            issue("duplicate_reward_page", "多个奖励页面共用同一确认操作；不能当作多次获得。", [confirmations[confirmation], page["start"]])
        confirmations[confirmation] = page["start"]
        if len(receives[page_id]) > 1:
            issue("duplicate_reward_receive", "同一奖励页多次领取输入；实际获得次数仍未确认。",
                  [page["start"], *receives[page_id]], count=len(receives[page_id]))
        expected = {c["candidate_id"] for c in start.get("layout", {}).get("candidates", [])}
        known = set()
        evaluated = set()
        for preview in page["previews"]:
            candidate_id = preview["event"].get("candidate", {}).get("candidate_id")
            if isinstance(observation(preview).get("identity_verified"), bool):
                evaluated.add(candidate_id)
            if observation(preview).get("identity_verified"):
                known.add(candidate_id)
        candidate_expected += len(expected)
        candidate_total += len(expected & evaluated)
        candidate_known += len(expected & known)
        page["unique_candidates"] = len(expected)
        page["unique_identities_verified"] = len(expected & known)
        page["unique_identities_evaluated"] = len(expected & evaluated)
        page["repeated_preview_attempts"] = max(0, len(page["previews"]) - len({p["event"].get("candidate", {}).get("candidate_id")
                                                                              for p in page["previews"]}))
        if previous_page:
            previous_start = previous_page["start"]["event"]
            digest = start.get("frame", {}).get("sha256")
            if digest and digest == previous_start.get("frame", {}).get("sha256"):
                issue("repeated_reward_screen", "相邻奖励页起始截图相同，可能是重试或重复采集；不能据此确认重复获得。",
                      [previous_page["start"], page["start"]], severity="warning")
        previous_page = page
        if not page["candidate_previews_complete"]:
            failure = ((page.get("end") or {}).get("event") or {}).get("failure_diagnostic")
            issue("reward_preview_incomplete", "奖励候选预览不齐。", [page["start"], *page["diagnostics"][-1:]], detail=failure)
        if not page["end"] or not page["receive"]:
            issue("reward_page_unfinished", "奖励页缺少结束或领取输入记录。", [page["start"]])
        restored = (page.get("restore") or {}).get("event") or {}
        received = (page.get("receive") or {}).get("event") or {}
        if not restored.get("verified") or any(e.get("selected_candidate_id") != start.get("selected_candidate_id")
                                               for e in (restored, received) if e):
            issue("reward_selection_unverified", "奖励实际领取选择与原选择不一致或未核对。",
                  [e for e in (page["start"], page["restore"], page["receive"]) if e])
        # 预览重试不是新候选，同画面也不能独立证明重复获得。
        page["actual_selection"] = {"candidate_id": received.get("selected_candidate_id"),
                                    "framework_action_succeeded": received.get("framework_action_succeeded"),
                                    "inventory_change_confirmed": None}

    terminal_events = [e["event"] for e in valid if e["event"]["kind"] == "produce_session_end"]
    normal = bool(session.get("status") == "ended" and session.get("stop_reason") == "returned_home"
                  and not session.get("failure_stop_observed") and terminal_events
                  and terminal_events[-1].get("terminal") and terminal_events[-1].get("reason") == "returned_home")
    gameplay_observed = bool(any(e["event"]["kind"] in ("produce_gameplay_entry", "produce_event_choice", "battle_entry") for e in valid))
    legacy = session.get("schema_version", 1) < 2 and "start_source" not in session
    eligible = gameplay_observed or legacy
    entry_modes = [b["entry"] for b in boundaries]
    mode = ("interrupted_resume" if len(session["segments"]) > 1 else
            "skip_preparation" if not session.get("start_observed") or "ProduceSkipPreparation" in entry_modes else "fresh_start")
    if not eligible:
        state = "preparation_only"
    elif normal:
        state = "normal_completion"
    elif session.get("failure_stop_observed"):
        state = "failed"
    elif session.get("status") == "recording":
        state = "open_or_abandoned"
    else:
        state = "interrupted"
    if not normal:
        issue("no_normal_end", "没有正常返回主页的结束证据。", valid[-1:], severity="warning", stop_reason=session.get("stop_reason"))
    if mode == "interrupted_resume":
        issue("resume_continuity_unproven", "续跑配置一致仅支持关联，不能独立证明为同一游戏存档。", severity="warning")
    if mode == "skip_preparation" or not session.get("start_observed"):
        issue("missing_preparation", "跳过准备或从中途采集，缺少整轮开局证据。", valid[:1], severity="warning")
    settlement_missing = {}
    for field, reading in report["final_settlement"].items():
        if reading["value"] is None:
            reason = ("conflicting_readings" if reading["conflict"] else "gameplay_not_started" if not eligible
                      else "no_settlement_capture" if not report["settlement_observations"]
                      else "no_analysis" if not any(observation(e) for e in report["settlement_observations"]) else "not_readable_in_saved_frames")
            settlement_missing[field] = reason
            issue("settlement_" + reason, f"结算字段 {field} 缺失：{reason}。", report["settlement_observations"][-2:],
                  severity="error" if reading["conflict"] else "warning")
    if report["evaluation_check"].get("status") == "mismatch":
        issue("evaluation_mismatch", "结算公式与实测评分不一致，需复核。", report["settlement_observations"][-2:])

    timeline = []
    kinds = {"preparation_observation", "produce_session_start", "produce_session_resume", "produce_gameplay_entry", "produce_session_end",
             "produce_event_choice", "produce_event_input_result", "route_before", "route_input_result", "route_after",
             "settlement_observation", "exam_result_observation"}
    for entry in valid:
        event = entry["event"]
        kind = event["kind"]
        if kind not in kinds and not kind.startswith("reward_"):
            continue
        detail = {key: event[key] for key in ("node", "operation", "decision_id", "reward_page_id", "selected", "selected_candidate_id",
                  "candidate", "candidate_id", "candidates", "policy_choice", "framework_action_succeeded", "actual_point", "reason", "terminal",
                  "verified", "failure_diagnostic", "stop_reason", "candidate_previews_complete", "safe_to_receive") if key in event}
        obs = observation(entry)
        if kind == "produce_event_choice":
            detail["policy_inputs_with_fallbacks"] = event.get("policy_inputs_with_fallbacks")
            detail["teacher_suggestion"] = event.get("teacher_suggestion")
        if kind == "route_before":
            detail["visible_candidates"] = [c.get("identity", {}).get("database_name") for c in obs.get("candidates", [])]
        if kind == "reward_page_start":
            detail["candidates"] = event.get("layout", {}).get("candidates")
        if kind == "reward_candidate_preview":
            detail.update(observed_name=obs.get("observed_name"), identity_verified=obs.get("identity_verified"),
                          effects_status=(obs.get("effects") or {}).get("semantic_status"))
        if kind in ("settlement_observation", "exam_result_observation"):
            detail["settlement"] = {k: v.get("value") for k, v in (obs.get("settlement") or {}).items()}
        timeline.append({"kind": kind, "detail": detail, "evidence": source(entry, capture_root)})
    timeline.sort(key=lambda row: (timestamp(row["evidence"]["time"]) or 0, row["evidence"]["run_id"], row["evidence"]["line"]))

    # 同一保存画面重复关联多个事件时只计一次手牌身份识别。
    hand_frames = {}
    analysis_frames = {}
    hand_known = hand_total = 0
    for entry in valid:
        if entry["event"]["kind"] not in ("battle_entry", "decision_before", "decision_after"):
            continue
        key = (entry["source"]["run_id"], entry["event"].get("frame", {}).get("sha256") or entry["event"]["event_id"])
        if entry["event"].get("frame"):
            analysis_frames[key] = analysis_frames.get(key, False) or bool(observation(entry) and not observation(entry).get("error"))
        previous = hand_frames.get(key)
        # 同画面优先使用带原始检测候选的操作前结果，不按识别成功数量挑结果。
        if previous is None or (entry["event"].get("candidates") and not previous["event"].get("candidates")):
            hand_frames[key] = entry
        elif not observation(previous) and observation(entry):
            hand_frames[key] = entry
    for entry in hand_frames.values():
        for card in observation(entry).get("cards", []):
            hand_total += 1
            hand_known += bool(card.get("identity", {}).get("database_card_id"))
    recognition = {"reward_identity": metric(candidate_known, candidate_total), "hand_identity": metric(hand_known, hand_total),
                   "reward_candidate_analysis": metric(candidate_total, candidate_expected),
                   "battle_frame_analysis": metric(sum(analysis_frames.values()), len(analysis_frames)),
                   "settlement_fields": metric(sum(r["value"] is not None for r in report["final_settlement"].values()),
                                               5 if report["settlement_observations"] else 0)}
    uncollected = {key: {"status": "未采集", "value": None} for key in
                   ("shop_purchases", "shop_cost", "strengthen_targets", "strengthen_cost", "full_initial_deck", "inventory_changes")}
    errors = sum(i["severity"] == "error" for i in issues)
    report.update(schema_version=2, timeline=timeline, produce_events=choices, recognition=recognition, uncollected=uncollected,
                  code_versions=sorted(versions),
                  integrity={"state": state, "entry_mode": mode, "round_eligible": eligible, "normal_completion": normal,
                             "issues": issues, "errors": errors, "settlement_missing_reasons": settlement_missing,
                             "segment_boundaries": boundaries,
                             "preparation_observations": len(report["preparation_observations"]),
                             "start_observed": session.get("start_observed", False),
                             "continuity": "configuration_only" if mode == "interrupted_resume" else "single_segment"})
    report["complete"] = bool(normal and session.get("start_observed") and report["settlement_fields_complete"] and not errors
                              and mode != "interrupted_resume")
    report["timing"]["active_seconds"] = active_seconds if duration_known and eligible else None
    report["timing"]["active_seconds_basis"] = "gameplay_entry_plus_resume_capture_intervals; excludes_pause; not_pure_game_time"
    report["outcome"]["success"] = False if session.get("failure_stop_observed") else None
    report["integrity"]["strategy_readiness"] = {
        "basic_comparison": "usable" if report["complete"] else "partial",
        "causal_strategy_analysis": "insufficient",
        "reason": "现有操作、候选与结算可用于描述性对比；完整库存、实际成本和强化变化未采集，不能推断策略收益或因果。"}
    return report


def cell(value):
    return str(value).replace("|", "/").replace("\n", " ").replace("\r", " ")


def evidence_links(evidence):
    path = Path(evidence["events_path"]).as_posix()
    links = [f"[事件日志 {evidence['line']}](<{path}:{evidence['line']}>)"]
    for key, label in (("frame_path", "截图"), ("analysis_path", "识别结果")):
        if evidence.get(key):
            links.append(f"[{label}](<{Path(evidence[key]).as_posix()}>)")
    return " / ".join(links)


def timeline_summary(row):
    """Markdown 展示简短摘要；完整参数保留在 JSON 中。"""
    kind, detail = row["kind"], row["detail"]
    if kind == "produce_event_choice":
        candidates = "、".join(str(name) for c in detail.get("candidates", []) for name in c)
        return f"候选：{candidates or '未记录'}；选择：{(detail.get('selected') or {}).get('name', '未知')}"
    if kind == "reward_candidate_preview":
        return (f"候选 {(detail.get('candidate') or {}).get('candidate_id')}：{detail.get('observed_name') or '未识别'}；"
                f"身份核对：{detail.get('identity_verified')}；效果来源：{detail.get('effects_status') or '未分析'}")
    if kind == "reward_page_start":
        return f"候选 {len(detail.get('candidates') or [])} 项；策略原选择：{detail.get('selected_candidate_id')}"
    if kind == "reward_selection_restored":
        return f"恢复选择：{detail.get('selected_candidate_id')}；核对：{detail.get('verified')}"
    if kind == "reward_page_end":
        return f"候选预览齐全：{detail.get('candidate_previews_complete')}；停止原因：{detail.get('stop_reason') or '无记录'}"
    if kind in ("produce_event_input_result", "route_input_result", "reward_receive_input", "reward_preview_input"):
        return (f"选择：{detail.get('selected_candidate_id') or detail.get('candidate_id') or detail.get('node') or detail.get('decision_id')}；"
                f"输入位置：{detail.get('actual_point')}；框架成功：{detail.get('framework_action_succeeded')}；游戏结果未确认")
    if kind in ("settlement_observation", "exam_result_observation"):
        values = "；".join(f"{k}={v}" for k, v in detail.get("settlement", {}).items() if v is not None)
        return values or "该画面未读出结算字段"
    if kind == "route_before":
        names = "、".join(v for v in detail.get("visible_candidates", []) if v)
        return f"{detail.get('node')} / {detail.get('operation')}；可见候选：{names or '未识别'}"
    return "；".join(f"{k}={v}" for k, v in detail.items() if v is not None) or "分段生命周期标记"


def render_audit(report):
    audit = report["integrity"]
    lines = ["", "## 整轮完整性与策略分析准备度", "",
             f"- 轮次状态：{audit['state']}；入口：{audit['entry_mode']}；正常完成：{audit['normal_completion']}；完整数据：{report['complete']}",
             f"- 记录区间耗时（排除续跑间隔）：{report['timing']['active_seconds']} 秒；整段墙钟跨度另见 timing。",
             f"- 代码指纹：{', '.join(report['code_versions'])}；指纹来自采集时文件 SHA，不使用当前工作区版本替代。",
             "- " + audit["strategy_readiness"]["reason"],
             "- 购物明细/实际扣费、强化目标/成本、完整开局牌组、库存变化：未采集（null），不能视为零或未发生。",
             "- 识别成功率按已保存样本计算，不是人工核验准确率；预览重试与同画面手牌不重复计入分母。", "",
             "| 识别对象 | 成功 / 样本 | 比例 |", "| --- | --- | --- |"]
    for key, value in report["recognition"].items():
        lines.append(f"| {key} | {value['success']} / {value['total']} | {percent(value['rate'])} |")
    lines.extend(["", "### 异常与缺失原因", ""])
    if not audit["issues"]:
        lines.append("- 未发现已实现审计规则覆盖的异常。")
    for item in audit["issues"]:
        refs = "；".join(evidence_links(e) for e in item["evidence"])
        if not refs:
            refs = (f"[会话清单](<{Path(item['session_path']).as_posix()}>)；"
                    + cell(item.get("events_path") or item.get("run_id") or "无对应截图或事件"))
        lines.append(f"- **{item['code']}** ({item['severity']})：{item['message']} {refs}")
    lines.extend(["", "## 可追溯过程（按观察时间）", "",
                  "选择与输入成功分开记录；实际获得和库存改变尚未确认。事件策略输入可能含既有兜底值，不能当作核验后的状态。",
                  "日志链接指向原始 events.jsonl 的行号；缓冲的准备观察使用 observed_at 排序。", "",
                  "| 时间 | 事件 | 摘要 | 证据 |", "| --- | --- | --- | --- |"])
    for row in report["timeline"]:
        summary = timeline_summary(row)
        lines.append(f"| {row['evidence']['time']} | {row['kind']} | {cell(summary)} | {evidence_links(row['evidence'])} |")
    return lines


def percent(value):
    return f"{value:.1%}" if value is not None else "未采集/无样本"


def distribution(values):
    values = sorted(v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
    n = len(values)
    return {"n": n, "mean": sum(values) / n if n else None,
            "median": (values[(n - 1) // 2] + values[n // 2]) / 2 if n else None,
            "min": values[0] if n else None, "max": values[-1] if n else None}


def summarize(reports):
    eligible = [r for r in reports if r["integrity"]["round_eligible"]]
    completed = [r for r in eligible if r["integrity"]["normal_completion"]]
    comparable = [r for r in completed if not r["integrity"]["errors"] and r["integrity"]["entry_mode"] != "interrupted_resume"]
    recognition = {}
    for key in ("reward_identity", "hand_identity", "settlement_fields", "battle_frame_analysis", "reward_candidate_analysis"):
        recognition[key] = metric(sum(r["recognition"][key]["success"] for r in reports), sum(r["recognition"][key]["total"] for r in reports))
    return {"sessions": len(reports), "eligible_rounds": len(eligible), "preparation_only": len(reports) - len(eligible),
            "completion": metric(len(completed), len(eligible)), "comparable_rounds": len(comparable),
            "complete_data": sum(r["complete"] for r in reports), "states": dict(Counter(r["integrity"]["state"] for r in reports)),
            "duration_seconds": distribution(r["timing"]["active_seconds"] for r in comparable),
            "settlement": {field: distribution(r["final_settlement"][field]["value"] for r in comparable) for field in ("score", "Vo", "Da", "Vi")},
            "recognition": recognition, "uncollected": {key: {"status": "未采集", "value": None} for key in
                                                          ("shop_purchases", "shop_cost", "strengthen_targets", "strengthen_cost")}}


def build_corpus(reports, output, failures=()):
    """按采集时配置身份分组；同一轮跨代码版本进入独立混合版本组。"""
    unique = {}
    duplicate_sessions = []
    for report in reports:
        session_id = report["session"]["session_id"]
        if session_id in unique:
            duplicate_sessions.append(session_id)
            continue
        unique[session_id] = report
    reports = list(unique.values())
    groups = defaultdict(list)
    breakdowns = {key: defaultdict(list) for key in ("scenario", "difficulty", "idol", "code_version")}
    for report in reports:
        binding = report["session"]["bindings"]
        difficulty = binding.get("difficulty")
        if isinstance(difficulty, list):
            difficulty = "/".join(difficulty)
        idol = binding.get("configured_idol") or {}
        version = "+".join(report["code_versions"]) or "未记录"
        dims = {"scenario": binding.get("scenario") or "未记录", "difficulty": difficulty or "未记录",
                "idol": idol.get("idol_name") or "未记录", "code_version": version}
        groups[tuple(dims.items())].append(report)
        for key, value in dims.items():
            breakdowns[key][value].append(report)
    shared_runs = defaultdict(list)
    for report in reports:
        for segment in report["segments"]:
            shared_runs[segment["run_id"]].append(report["session"]["session_id"])
    shared_runs = {k: v for k, v in shared_runs.items() if len(v) > 1}
    # 跨会话复用同一分段的轮次排除统计，防止整轮双计数。
    contaminated = {sid for ids in shared_runs.values() for sid in ids}
    usable = [r for r in reports if r["session"]["session_id"] not in contaminated]
    usable_ids = {r["session"]["session_id"] for r in usable}
    result = {"schema_version": 1, "summary": summarize(usable), "failed_sessions": list(failures),
              "duplicate_sessions_excluded": duplicate_sessions, "shared_segments_excluded": shared_runs,
              "groups": [{"dimensions": dict(key), **summarize([r for r in value if r["session"]["session_id"] in usable_ids])}
                         for key, value in sorted(groups.items())],
              "breakdowns": {key: [{"value": v, **summarize([r for r in rs if r["session"]["session_id"] in usable_ids])}
                                   for v, rs in sorted(values.items())]
                             for key, values in breakdowns.items()},
              "session_reports": [{"session_id": r["session"]["session_id"], "state": r["integrity"]["state"],
                                   "report_path": r.get("report_path")} for r in reports]}
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = result["summary"]
    lines = ["# 多轮培育基础统计", "",
             f"- 会话 {summary['sessions']}；实际进入培育 {summary['eligible_rounds']}；准备片段 {summary['preparation_only']}（不计完成率分母）。",
             f"- 正常返回主页 {summary['completion']['success']} / {summary['completion']['total']}，完成率 {percent(summary['completion']['rate'])}；完整数据 {summary['complete_data']} 轮。",
             f"- 耗时与数值统计使用正常结束且无审计错误的 {summary['comparable_rounds']} 轮；每项另列有效 n，null 不填零。",
             "- 完成率表示已观察到正常结束的比例；未结束、遗留 recording 和中断均保留状态，不等于游戏胜率。",
             "- 按配置偶像分组，不宣称实际身份已经核验。跨版本轮次单列混合指纹；识别算法版本另保存在分段 analysis 中。",
             "- 识别成功率是已保存样本的可读覆盖率，不是人工真值准确率；跨组比较应同时检查采集/分析版本与样本数。",
             "- 购物、强化明细与成本：未采集；现有数据支持描述性比较，暂不足以评价购物/强化收益及完整策略因果。", "",
             "| 剧本 | 难度 | 配置偶像 | 代码指纹 | 完成 / 进入 | 平均耗时秒 (n) | 评分均值 (n) | Vo / Da / Vi 均值 (n) | 奖励身份成功率 | 手牌身份成功率 | 结算字段成功率 |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for group in result["groups"]:
        d = group["dimensions"]
        def display(stat):
            return f"{stat['mean']:.1f} ({stat['n']})" if stat["n"] else "未采集 (0)"
        rates = [f"{percent(group['recognition'][key]['rate'])} ({group['recognition'][key]['success']}/{group['recognition'][key]['total']})"
                 for key in ("reward_identity", "hand_identity", "settlement_fields")]
        lines.append("| " + " | ".join([*[cell(d[k]) for k in ("scenario", "difficulty", "idol", "code_version")],
                     f"{group['completion']['success']} / {group['completion']['total']}", display(group["duration_seconds"]),
                     display(group["settlement"]["score"]), " / ".join(display(group["settlement"][k]) for k in ("Vo", "Da", "Vi")), *rates]) + " |")
    lines.extend(["", "## 单轮报告", ""])
    for item in result["session_reports"]:
        target = Path(item["report_path"]).as_posix() if item["report_path"] else ""
        lines.append(f"- [{item['session_id']}](<{target}>)：{item['state']}")
    for failure in failures:
        lines.append(f"- 报告生成失败（未纳入统计）：{cell(failure)}")
    for run, ids in shared_runs.items():
        lines.append(f"- 分段重复归属，相关会话已排除：{run} → {', '.join(ids)}")
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
