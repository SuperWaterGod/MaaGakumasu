"""培育单轮完整性审计与过程展示；不执行设备输入。"""

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


def analysis_records(path):
    """逐行读取识别结果；相同截图通过事件引用复用，不重复存整份结果。"""
    values = {}
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.endswith("\n"):
                break  # 后台尚未写完的尾行留待下一次报告读取。
            entry = json.loads(line)
            result = values[entry["result_ref"]] if entry.get("result_ref") else entry.get("observation")
            values[entry["event_id"]] = result
            yield {key: value for key, value in entry.items() if key != "observation"}, result


def source(entry, capture_root):
    """原始 JSONL 是可精确定位的过程日志，截图和派生结果另附路径。"""
    raw = entry["event"]
    location = entry["source"]
    run = Path(location["events_path"]).resolve().parent
    frame = raw.get("frame") or {}
    derived = (entry.get("derived") or {})
    result = "analysis/results.jsonl" if derived else None
    return {**location, "event_id": raw["event_id"], "time": raw.get("observed_at") or raw.get("time"),
            "frame_path": str((run / frame["path"]).resolve()) if frame.get("path") and frame.get("retention") != "temporary" else None,
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
                       "session_path": str(directory / "data/session.json"), **extra})

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
        if frame.get("retention") == "temporary":
            pass  # 用户选择不留图，派生结果仍保留；不视为意外丢帧。
        elif frame.get("path"):
            run = Path(entry["source"]["events_path"]).resolve().parent
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
    eligible = gameplay_observed
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
             "settlement_observation", "exam_result_observation", "decision_before", "decision_after", "move_cards_screen",
             "battle_entry", "battle_exit", "battle_invocation_start", "battle_invocation_end", "pipeline_action", "pipeline_failure", "option_choice", "exam_failure_or_retry"}
    for entry in valid:
        event = entry["event"]
        kind = event["kind"]
        if kind not in kinds and not kind.startswith("reward_"):
            continue
        detail = {key: event[key] for key in ("node", "operation", "battle_id", "decision_id", "reward_page_id", "selected", "selected_candidate_id",
                  "candidate", "candidate_id", "candidates", "policy_choice", "framework_action_succeeded", "actual_point", "reason", "terminal",
                  "action", "selected_box", "selection", "observations", "policy_inputs_with_fallbacks", "policy", "title", "state_changes",
                  "verified", "failure_diagnostic", "stop_reason", "candidate_previews_complete", "safe_to_receive") if key in event}
        obs = observation(entry)
        detail["state_changes"] = ((entry.get("derived") or {}).get("link") or {}).get("state_changes")
        if kind in ("decision_before", "decision_after", "battle_entry"):
            detail["battle_state"] = obs.get("battle_state")
            detail["cards"] = [{"name": card.get("identity", {}).get("database_name"), "box": card.get("box")}
                               for card in obs.get("cards", [])]
        if kind == "produce_event_choice":
            detail["policy_inputs_with_fallbacks"] = event.get("policy_inputs_with_fallbacks")
            detail["teacher_suggestion"] = event.get("teacher_suggestion")
        if kind == "route_before":
            detail["visible_candidates"] = [c.get("identity", {}).get("database_name") for c in obs.get("candidates", [])]
        if kind in ("route_before", "move_cards_screen"):
            link = ((entry.get("derived") or {}).get("link") or {})
            if "icon_matches" in obs or "icon_matches" in link:
                detail["icon_matches"] = obs.get("icon_matches", link.get("icon_matches"))
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
        candidates = "、".join(
            f"{name}_SP" if name in ("Vo", "Da", "Vi") and candidate.get("SP") is True else str(name)
            for candidate in detail.get("candidates", [])
            for name, box in candidate.items()
            if name != "SP" and isinstance(box, list) and len(box) == 4
        )
        return f"候选：{candidates or '未记录'}；选择：{(detail.get('selected') or {}).get('name', '未知')}"
    if kind == "decision_before":
        operation = {"play_card": "出牌", "use_drink_task": "使用饮料", "skip_round": "跳过回合", "move_cards": "移动卡牌"}.get(detail.get("operation"), "战斗操作")
        card = next((c.get("name") for c in detail.get("cards", []) if c.get("box") == detail.get("selected_box")), None)
        return f"{operation}：{card or '对象未识别'}；依据：{detail.get('reason') or '未记录'}"
    if kind == "decision_after":
        return "返回可操作状态或退出后的观察；动作效果须结合后续证据"
    if kind == "option_choice":
        return f"{detail.get('title')}－{detail.get('action')}；输入位置：{detail.get('actual_point')}"
    if kind == "pipeline_action":
        return f"{detail.get('node')}；框架执行成功：{detail.get('framework_action_succeeded')}"
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


def percent(value):
    return f"{value:.1%}" if value is not None else "未采集/无样本"

