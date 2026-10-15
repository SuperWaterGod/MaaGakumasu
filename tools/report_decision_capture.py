"""汇总采集证据，保留观察结果与实际动作标签的区别。"""

import json
import argparse
from pathlib import Path
from collections import Counter


def build_report(directory: Path):
    directory = directory.resolve()
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    events = []
    parse_errors = []
    for number, line in enumerate((directory / "events.jsonl").read_text(encoding="utf-8").splitlines(), 1):
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            parse_errors.append(number)
    counts = Counter(event["kind"] for event in events)
    frames = {event["frame"]["path"] for event in events if "frame" in event}
    decisions = {event["event_id"] for event in events if event["kind"] == "decision_before"}
    outcomes = {event.get("decision_id") for event in events if event["kind"] == "decision_after"}
    missing = []
    for frame in frames:
        path = (directory / frame).resolve()
        if not path.is_relative_to(directory) or not path.is_file():
            missing.append(frame)
    summary = {
        "run_id": manifest["run_id"],
        "mode": manifest["metadata"]["mode"],
        "status": manifest["status"],
        "event_counts": dict(counts),
        "unique_frames": len(frames),
        "missing_frames": sorted(missing),
        "invalid_json_lines": parse_errors,
        "decisions_without_after": sorted(decisions - outcomes),
        "orphan_after": [
            event["event_id"] for event in events if event["kind"] == "decision_after" and event.get("decision_id") not in decisions
        ],
        "events_dropped": manifest.get("events_dropped", 0),
        "battle_hint_observations": sum(event.get("battle_hint") is True for event in events),
        "game_actions_confirmed": sum(event.get("game_action_confirmed") is True for event in events),
        "unknown_state_fields": manifest.get("state_fields", {}),
        "card_identity_analysis": [
            json.loads(path.read_text(encoding="utf-8")) for path in sorted((directory / "derived/card_identity").glob("*/status.json"))
        ],
    }
    (directory / "report.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 出牌采集报告",
        "",
        f"- 采集 ID：`{manifest['run_id']}`",
        f"- 模式：`{summary['mode']}`；状态：`{summary['status']}`",
        f"- 事件：{len(events)}；独立截图：{len(frames)}；队列丢弃：{summary['events_dropped']}",
        f"- 操作前记录：{len(decisions)}；缺少后续观察：{len(decisions - outcomes)}",
        f"- 出牌场景提示帧：{summary['battle_hint_observations']}；已确认游戏动作：{summary['game_actions_confirmed']}",
        f"- 缺失截图：{len(missing)}；JSON 损坏行：{parse_errors}",
        "",
        "旁路采集没有实际点击标签。场景提示和返回可操作状态均不等于动作成功。",
        "卡牌身份、可见战斗状态与卡库参考效果见后台分析；未知字段、隐藏 Buff 和未适配布局不当作零。",
        "",
        "## 事件分布",
        "",
        "| 事件 | 数量 |",
        "| --- | --- |",
        *[f"| {kind} | {count} |" for kind, count in sorted(counts.items())],
        "",
    ]
    for analysis in summary["card_identity_analysis"]:
        lines.extend(
            [
                "## 卡牌身份分析",
                "",
                f"- 状态：{analysis['status']}；处理画面：{analysis['unique_frames_processed']}",
                f"- 匹配：{analysis['matched']}；牌名已匹配但强化待确认：{analysis['variant_unresolved']}；未知：{analysis['unknown']}",
                f"- 分析错误：{analysis['errors']}；卡库 SHA-256：`{analysis['catalog_sha256']}`",
                "",
            ]
        )
        if "state_frames" in analysis:
            lines.extend(
                [
                    f"- 可读出牌状态：{analysis['state_frames']} 帧；状态识别异常：{analysis['state_errors']} 帧",
                    "- 数值字段覆盖（按分析帧计）：" + "；".join(f"{key}={count}" for key, count in analysis["state_field_observations"].items()),
                    "",
                ]
            )
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(build_report(args.directory), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
