"""培育跨轮统计，不参与客户端运行。"""

import json
from pathlib import Path
from collections import Counter, defaultdict

from produce.audit import cell, metric, percent, evidence_links, timeline_summary


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
