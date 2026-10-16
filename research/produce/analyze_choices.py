"""从已验收的整轮报告建立描述性选择基线，不读取设备或调整策略。"""

import sys
import json
import hashlib
import argparse
from pathlib import Path
from collections import Counter, defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research.produce.produce_reporting import cell, metric, percent, evidence_links

POLICY_FILES = ("agent/produce/events.py", "agent/produce/battle.py", "agent/produce/options.py", "agent/custom/action/produce.py")
UNMEASURED = ("shop_purchases", "shop_cost", "strengthen_targets", "strengthen_cost", "full_initial_deck", "inventory_changes")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def trace(entry):
    event, source = entry["event"], entry["source"]
    frame = event.get("frame") or {}
    return {**source, "event_id": event["event_id"], "time": event.get("observed_at") or event.get("time"),
            "frame_path": str(Path(source["events_path"]).parent / frame["path"]) if frame.get("path") else None}


def snapshot(report):
    """只归纳记录中可见的候选和选择，保留身份未知与选择未核对的区别。"""
    session = report["session"]
    binding = session["bindings"]
    configured = binding.get("configured_idol") or {}
    difficulty = binding.get("difficulty")
    dimensions = {"scenario": binding.get("scenario"), "difficulty": "/".join(difficulty) if isinstance(difficulty, list) else difficulty,
                  "idol": configured.get("idol_name"), "song": configured.get("song_name"),
                  "code_version": "+".join(report.get("code_versions", [])) or "未记录"}
    settings = binding.get("node_settings")
    policy_versions = set()
    for segment in report.get("segments", []):
        files = (segment.get("manifest", {}).get("metadata") or {}).get("version_files") or {}
        if not all(name in files for name in POLICY_FILES):
            policy_versions.add(None)
            continue
        policy_files = {name: digest for name, digest in files.items() if name in POLICY_FILES or "/pipeline/Produce" in name}
        policy_versions.add(fingerprint(policy_files))
    policy_version = next(iter(policy_versions)) if len(policy_versions) == 1 and None not in policy_versions else None
    events, rewards = [], []
    for pair in report.get("produce_events", {}).values():
        entry = pair["choice"]
        raw = entry["event"]
        selected = raw.get("selected") or {}
        options, chosen = [], None
        for candidate in raw.get("candidates", []):
            for name, box in candidate.items():
                if name == "SP" or not isinstance(box, list) or len(box) != 4:
                    continue
                label = name + "_SP" if name in ("Vo", "Da", "Vi") and candidate.get("SP") is True else name
                options.append(label)
                if selected.get("name", "").removesuffix("_SP") == name and selected.get("box") == box:
                    if not selected.get("name", "").endswith("_SP") or candidate.get("SP") is True:
                        chosen = label
        events.append({"available": sorted(set(options)), "selected": chosen, "raw_selected": selected.get("name"),
                       "framework_action_succeeded": ((pair.get("input") or {}).get("event") or {}).get("framework_action_succeeded"),
                       "teacher_suggestion": raw.get("teacher_suggestion"), "evidence": trace(entry),
                       "policy_inputs_status": "may_contain_runtime_fallbacks; not_independently_verified"})
    for page_id, page in report.get("reward_pages", {}).items():
        start = page["start"]["event"]
        expected = {candidate["candidate_id"] for candidate in start.get("layout", {}).get("candidates", [])}
        previews = defaultdict(list)
        for entry in page["previews"]:
            previews[entry["event"].get("candidate", {}).get("candidate_id")].append(entry)
        candidates = []
        for candidate_id in sorted(expected):
            readings = []
            for entry in previews[candidate_id]:
                observation = (entry.get("derived") or {}).get("observation") or {}
                identity = observation.get("identity") or {}
                catalog_id = identity.get("catalog_id") or identity.get("database_card_id") or identity.get("database_item_id")
                if observation.get("identity_verified") is True and catalog_id:
                    readings.append((entry, observation, identity, str(catalog_id)))
            ids = {reading[3] for reading in readings}
            reading = readings[0] if len(ids) == 1 else None
            candidates.append({"candidate_id": candidate_id, "catalog_id": reading[3] if reading else None,
                               "item_kind": reading[1].get("item_kind") if reading else None,
                               "name": reading[2].get("database_name") or reading[1].get("observed_name") if reading else None,
                               "unknown_reason": None if reading else "identity_conflict" if ids else "not_verified",
                               "evidence": trace(reading[0]) if reading else trace(page["start"])})
        restore = (page.get("restore") or {}).get("event") or {}
        receive = (page.get("receive") or {}).get("event") or {}
        selected_id = receive.get("selected_candidate_id")
        verified = bool(restore.get("verified") and receive.get("framework_action_succeeded") is True
                        and selected_id in expected and selected_id == restore.get("selected_candidate_id") == start.get("selected_candidate_id"))
        rewards.append({"page_id": page_id, "candidates": candidates, "selected_candidate_id": selected_id,
                        "selection_input_verified": verified, "inventory_change_confirmed": None,
                        "policy_node": (start.get("policy_choice") or {}).get("policy_node"), "evidence": trace(page["start"])})
    return {"session_id": session["session_id"], "started_at": report["timing"]["started_at"], "dimensions": dimensions,
            "task_settings_version": fingerprint(settings) if settings else None, "policy_version": policy_version,
            "events": events, "rewards": rewards, "recognition": report["recognition"], "timing": report["timing"],
            "settlement": {k: v["value"] for k, v in report["final_settlement"].items()},
            "fan_votes": report.get("final_fan_votes", {}).get("value"), "formula": report.get("evaluation_check", {}),
            "report_path": report.get("report_path"), "uncollected": {key: {"status": "未采集", "value": None} for key in UNMEASURED}}


def summarize(sessions):
    event_offers, event_selected, event_evaluable = Counter(), Counter(), Counter()
    item_offers, item_selected, item_evaluable, item_names, item_kinds, examples = Counter(), Counter(), Counter(), {}, {}, {}
    reward_choices = Counter()
    for session in sessions:
        for event in session["events"]:
            event_offers.update(event["available"])
            if event["selected"] is not None:
                event_selected[event["selected"]] += 1
                event_evaluable.update(event["available"])
        for page in session["rewards"]:
            # 每页每个库条目只计一次，预览重试和重复槽位都不增加出现分母。
            offered = {c["catalog_id"]: c for c in page["candidates"] if c["catalog_id"] is not None}
            item_offers.update(offered.keys())
            for key, candidate in offered.items():
                item_names[key], item_kinds[key] = candidate["name"], candidate["item_kind"]
                examples.setdefault(key, candidate["evidence"])
            if page["selection_input_verified"]:
                item_evaluable.update(offered.keys())
                selected = next(c for c in page["candidates"] if c["candidate_id"] == page["selected_candidate_id"])
                if selected["catalog_id"]:
                    item_selected[selected["catalog_id"]] += 1
                    reward_choices[selected["item_kind"]] += 1
    return {"sessions": len(sessions), "event_decisions": sum(len(s["events"]) for s in sessions),
            "unknown_event_choices": sum(e["selected"] is None for s in sessions for e in s["events"]),
            "reward_pages": sum(len(s["rewards"]) for s in sessions),
            "reward_candidates": sum(len(p["candidates"]) for s in sessions for p in s["rewards"]),
            "unknown_reward_candidates": sum(c["catalog_id"] is None for s in sessions for p in s["rewards"] for c in p["candidates"]),
            "verified_reward_selection_inputs": sum(p["selection_input_verified"] for s in sessions for p in s["rewards"]),
            "unknown_selected_reward_identities": sum(
                p["selection_input_verified"] and any(c["candidate_id"] == p["selected_candidate_id"] and c["catalog_id"] is None
                                                     for c in p["candidates"]) for s in sessions for p in s["rewards"]),
            "event_selection_counts": dict(event_selected), "reward_selection_kinds": dict(reward_choices),
            "reward_policy_node_counts": dict(Counter(p["policy_node"] or "未记录" for s in sessions for p in s["rewards"])),
            "event_opportunities": [{"name": name, "offered_decisions": event_offers[name],
                                     "selection_when_available": metric(event_selected[name], event_evaluable[name])} for name in sorted(event_offers)],
            "reward_opportunities": [{"catalog_id": key, "name": item_names[key], "item_kind": item_kinds[key],
                                      "offered_pages": item_offers[key], "selection_when_offered": metric(item_selected[key], item_evaluable[key]),
                                      "example": examples[key]} for key in sorted(item_offers)],
            "teacher_suggestion_nonempty": sum(bool(e["teacher_suggestion"]) for s in sessions for e in s["events"]),
            "uncollected": {key: {"status": "未采集", "value": None} for key in UNMEASURED}}


def comparison(sessions):
    """对相同配置和已记录策略源码的端点差异作公式分解，不作动作收益归因。"""
    ordered = sorted(sessions, key=lambda session: session["started_at"])
    first, last = ordered[0], ordered[-1]
    def changes(a, b):
        return {key: {"before": a.get(key, 0), "after": b.get(key, 0)} for key in sorted(set(a) | set(b)) if a.get(key, 0) != b.get(key, 0)}
    def delta(a, b):
        return b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
    event_before, event_after = (Counter(e["selected"] for e in s["events"] if e["selected"] is not None) for s in (first, last))
    formula_before, formula_after = ((s["formula"].get("calculated") or {}) for s in (first, last))
    return {"session_ids": [s["session_id"] for s in sessions], "compared_endpoints": [first["session_id"], last["session_id"]],
            "dimensions": {key: value for key, value in first["dimensions"].items() if key != "code_version"},
            "full_code_versions": sorted({s["dimensions"]["code_version"] for s in sessions}),
            "policy_version": first["policy_version"], "task_settings_version": first["task_settings_version"],
            "score_delta": delta(first["settlement"]["score"], last["settlement"]["score"]),
            "parameter_delta": {k: delta(first["settlement"][k], last["settlement"][k]) for k in ("Vo", "Da", "Vi")},
            "formula_component_delta": {k: delta(formula_before.get(k), formula_after.get(k)) for k in
                                        ("parameter_rating", "vote_rating", "placement_rating", "exam_rating")},
            "event_count_differences": changes(event_before, event_after),
            "interpretation": "仅为首末记录端点差异；采集版本、随机候选、战斗过程及未知状态不同，不能归因为某张卡或一次课程。"}


def build_baseline(reports, output):
    sessions, excluded, seen, rejected_ids = [], [], {}, set()
    for report in reports:
        session_id = report["session"]["session_id"]
        if session_id in seen:
            excluded.append({"session_id": session_id, "reason": "duplicate_session"})
            continue
        seen[session_id] = report
        if not report.get("complete") or not report["integrity"].get("normal_completion") or report["integrity"].get("errors"):
            excluded.append({"session_id": session_id, "reason": "not_a_complete_normal_round"})
            continue
        sessions.append(snapshot(report))
    runs = defaultdict(set)
    for session in sessions:
        for segment in seen[session["session_id"]].get("segments", []):
            runs[segment["run_id"]].add(session["session_id"])
    for ids in runs.values():
        if len(ids) > 1:
            rejected_ids.update(ids)
    excluded.extend({"session_id": session_id, "reason": "shared_segment"} for session_id in sorted(rejected_ids))
    sessions = [s for s in sessions if s["session_id"] not in rejected_ids]
    groups, matched = defaultdict(list), defaultdict(list)
    for session in sessions:
        groups[tuple(session["dimensions"].items())].append(session)
        if session["policy_version"] and session["task_settings_version"]:
            identity = tuple((k, v) for k, v in session["dimensions"].items() if k != "code_version")
            matched[(identity, session["policy_version"], session["task_settings_version"])].append(session)
    summary = summarize(sessions)
    event_status = ("无样本" if not summary["event_decisions"] else
                    "部分可用" if summary["unknown_event_choices"] else "可用")
    reward_status = ("无样本" if not summary["reward_pages"] else
                     "部分可用" if summary["unknown_reward_candidates"] or
                     summary["verified_reward_selection_inputs"] < summary["reward_pages"] else "可用")
    readiness = [
        {"question": "实际记录中有哪些事件候选、选择与输入", "status": event_status,
         "scope": "按候选出现机会统计选择频率，不是策略优劣或实际收益。"},
        {"question": "奖励提供了什么、恢复并提交了哪个选择", "status": reward_status,
         "scope": "身份未知不强行合并；领取输入不等于库存变更。"},
        {"question": "最终评分、评级、三围与耗时", "status": "可用" if sessions else "无样本", "scope": "仅已完整结束的报告；按配置卡片与采集时代码分组。"},
        {"question": "老师建议是否影响选择", "status": "部分可用" if summary["teacher_suggestion_nonempty"] else "证据不足",
         "scope": "空字符串不能区分未启用、无建议与读取失败；不能当作建议未发生。"},
        {"question": "某个选择能提高多少评分", "status": "证据不足", "scope": "缺少对照实验、完整局内状态和可信动作收益归因；不训练或更改策略。"},
        *[{"question": key, "status": "未采集", "value": None, "scope": "缺少完整业务证据，不能填零。"} for key in UNMEASURED],
    ]
    result = {"schema_version": 1, "scope": "descriptive_selection_baseline_from_complete_reports", "summary": summary,
              "sessions": sessions, "excluded": excluded,
              "groups": [{"dimensions": dict(key), **summarize(value)} for key, value in sorted(groups.items(), key=lambda item: str(item[0]))],
              "matched_configuration_observations": [comparison(value) for value in matched.values() if len(value) > 1],
              "readiness": readiness, "device_input_sent": False, "fresh_ocr": False,
              "provenance": {"analyzer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                             "input_report_files": {s["session_id"]: seen[s["session_id"]].get("_baseline_input_source") for s in sessions},
                             "input_report_provenance": {s["session_id"]: seen[s["session_id"]].get("report_provenance") for s in sessions}}}
    output.mkdir(parents=True, exist_ok=True)
    (output / "baseline.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# 培育选择描述性基线与数据准备度", "",
             f"纳入 {len(sessions)} 个完整会话，记录 {summary['event_decisions']} 次培育事件、{summary['reward_pages']} 个奖励页和 {summary['reward_candidates']} 个唯一页内候选。",
             "这是现有自动策略的行为记录。选择频率不表示最优策略；候选默认效果、点击成功和实际获得分别看待。", "",
             "## 分组边界", "", "| 剧本 / 难度 | 配置偶像 / 歌曲 | 完整代码指纹 | 轮数 | 事件 / 奖励页 |", "| --- | --- | --- | --- | --- |"]
    for group in result["groups"]:
        d = group["dimensions"]
        lines.append(f"| {cell(d['scenario'])} / {cell(d['difficulty'])} | {cell(d['idol'])} / {cell(d['song'])} | {d['code_version']} | {group['sessions']} | {group['event_decisions']} / {group['reward_pages']} |")
    lines.extend(["", "## 单轮事件与奖励选择", ""])
    for session in sorted(sessions, key=lambda s: s["started_at"]):
        d = session["dimensions"]
        counts = summarize([session])
        v = session["settlement"]
        lines.extend([f"### {cell(d['scenario'])} / {cell(d['difficulty'])} / {cell(d['song'])} / {session['session_id'][:8]}", "",
                      f"- 原报告：[查看证据](<{Path(session['report_path']).as_posix()}>)" if session["report_path"] else "- 原报告路径未记录。",
                      f"- 结算：{v['score']} / {v['rank']}；Vo/Da/Vi：{v['Vo']} / {v['Da']} / {v['Vi']}。",
                      "- 事件选择次数：" + "；".join(f"{k}={v}" for k, v in counts["event_selection_counts"].items()),
                      "- 奖励选择输入类型：" + "；".join(f"{k}={v}" for k, v in counts["reward_selection_kinds"].items()),
                      "- 奖励选择入口：" + "；".join(f"{k}={v}" for k, v in counts["reward_policy_node_counts"].items()),
                      "- 读取覆盖（不是人工真值准确率）：" + "；".join(
                          f"{key}={value['success']}/{value['total']} ({percent(value['rate'])})"
                          for key, value in session["recognition"].items()
                          if key in ("hand_identity", "reward_identity", "settlement_fields")),
                      "", "| 事件 | 出现机会 | 有效选择机会内的选择次数 / 分母 |", "| --- | --- | --- |"])
        for item in counts["event_opportunities"]:
            m = item["selection_when_available"]
            lines.append(f"| {cell(item['name'])} | {item['offered_decisions']} | {m['success']} / {m['total']} ({percent(m['rate'])}) |")
        lines.extend(["", "奖励按页内唯一库条目统计出现机会；下表展示出现最多的 12 项，完整结果与逐条证据见 baseline.json。", "",
                      "| 奖励 | 类型 | 出现页数 | 已核对选择中的次数 / 出现机会 | 示例证据 |", "| --- | --- | --- | --- | --- |"])
        for item in sorted(counts["reward_opportunities"], key=lambda i: (-i["offered_pages"], i["catalog_id"]))[:12]:
            m = item["selection_when_offered"]
            lines.append(f"| {cell(item['name'])} | {item['item_kind']} | {item['offered_pages']} | {m['success']} / {m['total']} ({percent(m['rate'])}) | {evidence_links(item['example'])} |")
    lines.extend(["", "## 相同配置与策略源码的观察对照", ""])
    if not result["matched_configuration_observations"]:
        lines.append("没有足够的相同配置与已知策略源码样本；不把缺失指纹当成相同版本。")
    for pair in result["matched_configuration_observations"]:
        lines.extend([f"- 首末会话：{' → '.join(pair['compared_endpoints'])}；评分差（后减前）：{pair['score_delta']}。",
                      f"- 三围差：{pair['parameter_delta']}；评分公式分项差：{pair['formula_component_delta']}。",
                      f"- 事件选择次数差异：{pair['event_count_differences']}。",
                      f"- 完整代码指纹：{', '.join(pair['full_code_versions'])}；策略源码指纹：{pair['policy_version']}；已记录配置指纹：{pair['task_settings_version']}。",
                      "- " + pair["interpretation"]])
    lines.extend(["", "## 数据准备度", "", "| 分析问题 / 字段 | 状态 | 解释 |", "| --- | --- | --- |"])
    for item in readiness:
        lines.append(f"| {cell(item['question'])} | {item['status']} | {item['scope']} |")
    lines.extend(["", "策略指纹仅覆盖登记的事件、出牌、选项、框架动作入口与 Produce 流水线；配置指纹仅覆盖已采集的 node_settings，不能代表所有游戏内设置。",
                  "偶像和歌曲来自任务配置，完整开局牌组、角色等级、随机事件与隐含状态没有因此得到核验。空建议、未知身份和未知成本不当作零或未发生。",
                  "", "当前数据可用于选择分布、候选机会、识别覆盖和结算回溯；购物/强化收益与完整策略因果仍缺少证据。"])
    (output / "baseline.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="已验收会话 report.json 的父目录，每个会话一个子目录")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reports = []
    for path in sorted(args.directory.glob("*/report.json")):
        content = path.read_bytes()
        report = json.loads(content)
        report["_baseline_input_source"] = {"path": str(path.resolve()), "sha256": hashlib.sha256(content).hexdigest()}
        reports.append(report)
    result = build_baseline(reports, args.output)
    print(json.dumps({"sessions": len(result["sessions"]), "excluded": result["excluded"],
                      "event_decisions": result["summary"]["event_decisions"], "reward_pages": result["summary"]["reward_pages"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
