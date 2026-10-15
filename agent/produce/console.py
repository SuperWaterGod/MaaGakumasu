"""统一整理并按顺序展示出牌、奖励和结算摘要。"""

import re
from itertools import groupby

from .state import STANCE_NAMES, state_changes
from .settlement import FIELDS, evaluation_check

POLICY_NAMES = {
    "ProduceChooseFirst": "第一项",
    "ProduceChooseRecommend": "游戏推荐",
    "ProduceChooseEventRecommend": "活动推荐",
    "ProduceChooseSkip": "跳过奖励",
}

def policy_name(choice):
    return POLICY_NAMES.get((choice or {}).get("policy_node"), "原选择")


class SettlementConsole:
    """每个采集分段在结束画面处理完成后输出一条折叠结算摘要。"""

    def __init__(self, bindings):
        self.bindings = bindings
        self.readings = {key: [] for key in (*FIELDS, "fan_votes", "exam_score", "place")}
        self.frames = 0
        self.sent = False
        self.ending_observed = False

    def observe(self, event, observation):
        self.frames += 1
        self.ending_observed |= (event.get("kind") == "settlement_observation" or
                                 any(reading.get("value") is not None for reading in (observation.get("final_exam") or {}).values()))
        fields = {**(observation.get("settlement") or {}), "fan_votes": observation.get("fan_votes") or {},
                  "exam_score": (observation.get("final_exam") or {}).get("score") or {},
                  "place": (observation.get("final_exam") or {}).get("place") or {}}
        for key, reading in fields.items():
            if key in self.readings and reading.get("value") is not None:
                self.readings[key].append({**reading, "event_id": event.get("event_id")})

    def finish(self, event_id=None):
        if self.sent or not self.frames or not self.ending_observed:
            return None
        self.sent = True
        fields = {}
        for key, readings in self.readings.items():
            values = {reading["value"] for reading in readings}
            fields[key] = {"value": next(iter(values)) if len(values) == 1 else None,
                           "conflict": len(values) > 1, "observations": readings}
        final_exam = {"score": fields["exam_score"], "place": fields["place"]}
        check = evaluation_check(self.bindings.get("scenario"), fields, fields["fan_votes"], final_exam,
                                 self.bindings.get("difficulty"))
        scenario = self.bindings.get("scenario") or "模式未确认"
        difficulty = self.bindings.get("difficulty") or []
        difficulty = "/".join(difficulty) if isinstance(difficulty, list) else str(difficulty)
        status = {"match": "一致", "mismatch": "不一致", "calculated_only": "仅计算", "unavailable": "待补齐"}[check["status"]]
        show = lambda value: "未读到" if value is None else str(value)
        observed = check["observed_score"]
        calculated = check["calculated"] or {}
        formula = (f"公式计算：{show(calculated.get('score'))}｜推算评级 {show(calculated.get('rank'))}" if calculated else
                   "公式校验：暂不可用，输入缺项或不适用；画面评分与评级见标题")
        lines = [f"{scenario} {difficulty}｜{status}｜评分 {show(observed)}｜评级 {show(check['observed_rank'])}",
                 formula,
                 "最终属性：" + " / ".join(f"{key} {show(fields[key]['value'])}" for key in ("Vo", "Da", "Vi"))]
        if scenario == "初":
            lines.append(f"最终考试：{show(fields['exam_score']['value'])} 分｜名次 {show(fields['place']['value'])}")
        else:
            lines.append(f"最终粉丝投票数：{show(fields['fan_votes']['value'])}")
        if observed is not None and calculated.get("score") is not None:
            lines.append(f"评分差值（画面－公式）：{observed - calculated['score']:+d}")
        missing = [key for key, value in check["inputs"].items() if value is None]
        names = {"exam_score": "考试分数", "place": "考试名次", "fan_votes": "最终投票数"}
        if missing:
            lines.append("待补齐：" + "、".join(names.get(key, key) for key in missing))
        if any(reading["conflict"] for reading in fields.values()):
            lines.append("部分画面读数冲突，已保留未知；不按公式覆盖原始结果")
        lines.append("已使用最终三维，名次属性奖励不重复增加。" if scenario == "初" else
                     "公式值与画面值分开保留，完整证据见采集记录。")
        return {"message": "\n".join(lines), "scope": "settlement", "event_id": event_id, "evaluation_check": check}


DISPLAY_FIELDS = {"goodImpressionTurns": "好印象", "goodConditionTurns": "好调", "perfectConditionTurns": "绝好调",
                  "cardUsesRemaining": "使用次数", "turnsRemaining": "剩余回合", "halfCostTurns": "体力消耗减少回合",
                  "doubleCostTurns": "体力消耗增加回合", "concentration": "集中", "motivation": "干劲", "genki": "元气",
                  "stamina": "体力", "score": "基础参数", "cost": "通用消耗"}
SPECIAL_EFFECT = "特殊效果（详见数据记录）"


def _dsl_parts(text):
    """拆分展示语句时保留嵌套条件与触发条件。"""
    parts, start, depth = [], 0, 0
    for index, character in enumerate(text):
        if character in "{([":
            depth += 1
        elif character in "})]":
            depth -= 1
            if depth < 0:
                return None
        elif character == ";" and depth == 0:
            parts.append(text[start:index].strip())
            start = index + 1
    if depth:
        return None
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _comparison(text):
    match = re.fullmatch(r"(\w+)(>=|<=|==|>|<)([0-9]+(?:\.[0-9]+)?)", text.strip())
    if not match or match[1] not in DISPLAY_FIELDS:
        return None
    operator = {">=": "≥", "<=": "≤", "==": "＝", ">": "＞", "<": "＜"}[match[2]]
    unit = "回合" if match[1] in ("goodConditionTurns", "perfectConditionTurns") else ""
    return f"{DISPLAY_FIELDS[match[1]]}{operator}{match[3]}{unit}"


def _display_clause(text, depth):
    if depth > 3:
        return SPECIAL_EFFECT
    condition = re.fullmatch(r"if:(.+?)\s*\{(.*)\}", text)
    if condition:
        label = _comparison(condition[1])
        return f"{label}时（{_display_dsl(condition[2], depth + 1)}）" if label else SPECIAL_EFFECT
    trigger = re.fullmatch(r"at:(\w+)(?:\[(\w+)\])?\s*\{(.*)\}", text)
    if trigger:
        timing = {"afterStartOfTurn": "后续回合开始后", "startOfTurn": "后续回合开始时", "endOfTurn": "后续回合结束时",
                  "activeCardUsed": "主动卡使用时", "mentalCardUsed": "精神卡使用时", "cardUsed": "使用卡牌时"}
        label = timing.get(trigger[1])
        if trigger[2]:
            label = {"active": "主动卡使用时", "mental": "精神卡使用时"}.get(trigger[2]) if trigger[1] == "cardUsed" else None
        return f"{label}（{_display_dsl(trigger[3], depth + 1)}）" if label else SPECIAL_EFFECT
    change = re.fullmatch(r"(\w+)(\+=|-=|\*=|/=)([0-9]+(?:\.[0-9]+)?)", text)
    if change and change[1] in DISPLAY_FIELDS:
        return DISPLAY_FIELDS[change[1]] + {"+=": "＋", "-=": "−", "*=": "×", "/=": "÷"}[change[2]] + change[3]
    if text == "drawCard":
        return "抽牌1张"
    limit = re.fullmatch(r"limit:([0-9]+)", text)
    if limit:
        return f"最多触发{limit[1]}次"
    modifier = re.fullmatch(r"(setConcentrationEffectBuff|setGoodImpressionTurnsBuff)\(([0-9]+(?:\.[0-9]+)?),\s*([0-9]+)\)", text)
    if modifier:
        label = "集中效果提升" if modifier[1] == "setConcentrationEffectBuff" else "好印象效果提升"
        return f"{label}{float(modifier[2]) * 100:g}%（{modifier[3]}回合）"
    return SPECIAL_EFFECT


def _display_dsl(text, depth=0):
    """仅格式化展示，不解析 OCR、计算实际收益或修改库数据。"""
    parts = _dsl_parts(text.strip())
    if parts is None:
        return SPECIAL_EFFECT
    lines = []
    for clause, repeats in groupby(parts):
        count = sum(1 for _ in repeats)
        label = _display_clause(clause, depth)
        lines.append(label + (f"（共{count}次）" if count > 1 else ""))
    return "；".join(lines)


def catalog_lines(identity):
    """简要展示库默认效果；不支持的 DSL 保留在 JSON 中。"""
    dsl = identity.get("reference_effect_dsl") or {}
    result = []
    if dsl.get("conditions"):
        result.append("使用条件：" + (_comparison(str(dsl["conditions"])) or SPECIAL_EFFECT))
    if dsl.get("cost"):
        result.append("消耗：" + _display_dsl(str(dsl["cost"])))
    for field in ("actions", "effects"):
        if dsl.get(field):
            result.append(_display_dsl(str(dsl[field])))
    if str(dsl.get("limit", "")).isdigit():
        result.append(f"每课限用{dsl['limit']}次")
    elif dsl.get("limit"):
        result.append("特殊使用限制（详见数据记录）")
    return result


def state_lines(state):
    if not state or not state.get("hud_present"):
        return ["状态：当前画面未确认出牌 HUD"]
    fields = state["fields"]

    def value(key):
        return fields[key]["value"] if fields[key]["value"] is not None else "?"

    lines = [f"状态：剩 {value('turns_remaining')} 回合｜分 {value('score')}｜体 {value('stamina')}｜元 {value('genki')}"]
    lines.append(f"属性：{value('attribute')} {value('attribute_multiplier_percent')}%")
    anomaly = state.get("anomaly", {})
    if anomaly.get("held_cards_count", {}).get("value") is not None:
        capacity = anomaly["held_cards_capacity"]["value"]
        lines.append(f"保留：{anomaly['held_cards_count']['value']}/{capacity if capacity is not None else '?'}")
    if anomaly.get("hud_present"):

        def observed(key):
            return anomaly[key]["value"] if anomaly[key]["value"] is not None else "?"

        stance = STANCE_NAMES.get(observed("stance"), observed("stance"))
        stage = f" {observed('stance_stage')}阶段" if observed("stance") in ("strength", "preservation") else ""
        lines.append(f"非凡：{stance}{stage}｜全力值 {observed('full_power_charge')}")
        if anomaly["next_stance"]["value"] is not None:
            target = STANCE_NAMES.get(observed("next_stance"), observed("next_stance"))
            condition = anomaly["pointer_condition"]["value"]
            lines.append(f"指针预告：{observed('transition_after_turns')}回合后→{target}" + (f"｜{condition}" if condition else ""))
    descriptions = []
    for buff in state["buffs"]:
        suffix = {"percent": "%", "turns": "回合", "times": "次"}.get(buff["unit"], "")
        number = buff["value"] if buff["value"] is not None else "?"
        descriptions.append(f"{buff['name']} {number}{suffix}")
    for index in range(0, len(descriptions), 3):
        lines.append("Buff：" + "｜".join(descriptions[index : index + 3]))
    return lines


def card_name(identity):
    if identity.get("database_card_id"):
        name = identity.get("base_name", identity["database_name"].rstrip("+")) + "+" * identity.get("observed_plus_count", 0)
    else:
        name = identity.get("observed_name", "") or "牌名未识别"
    suffix = " [未知]" if identity["status"] == "unknown" else ""
    if identity.get("match_method") == "prefix":
        suffix = " ≈"
    return " ".join(name.split()) + suffix


def hand_summary(cards, selected_box=None):
    parts = []
    for index, card in enumerate(sorted(cards, key=lambda card: card["box"][0]), 1):
        label = {"cards": "", "suggestions": "荐 ", "useless": "禁 "}.get(card.get("label"), "? ")
        marker = "→ " if card["box"] == selected_box else ""
        # 避免 MFA 的 HTML 解析器将内容误当作 Markdown 有序列表。
        parts.append(f"{index}｜{marker}{label}{card_name(card['identity'])}")
    return "\n".join(parts) if parts else "未检测到手牌"


def after_console(event, before, result):
    state = result.get("battle_state")
    changes = state_changes(before, state)
    if not state or not state.get("hud_present"):
        return f"操作后 #{event['seq']}：当前画面未确认出牌 HUD", changes
    labels = {
        "turns_remaining": "剩余回合",
        "score": "分数",
        "stamina": "体力",
        "genki": "元气",
        "attribute": "属性",
        "attribute_multiplier_percent": "倍率",
        "goodImpressionTurns": "好印象",
        "concentration": "集中",
        "motivation": "干劲",
    }
    labels.update({buff["key"]: buff["name"] for buff in state.get("buffs", [])})
    labels.update(
        {
            "anomaly_stance": "姿态",
            "anomaly_stance_stage": "姿态阶段",
            "anomaly_full_power_charge": "全力值",
            "anomaly_next_stance": "预告姿态",
            "anomaly_transition_after_turns": "距切换回合",
            "anomaly_held_cards_count": "保留牌数",
            "anomaly_held_cards_capacity": "保留容量",
        }
    )

    def display(value):
        return STANCE_NAMES.get(value, value) if isinstance(value, str) else value

    parts = []
    score = changes.get("score")
    if score:
        delta = score["after"] - score["before"]
        parts.append(f"分数 {score['before']:,} → {score['after']:,}（{delta:+,}）")
    remaining = []
    for key, value in changes.items():
        if key == "score":
            continue
        unit = "%" if key == "attribute_multiplier_percent" else ""
        remaining.append(f"{labels.get(key, key)} {display(value['before'])}{unit} → {display(value['after'])}{unit}")
    parts.extend("｜".join(remaining[index : index + 2]) for index in range(0, len(remaining), 2))
    if not parts:
        return None, changes
    decision = str(event.get("decision_id") or "?").rsplit(":", 1)[-1]
    return f"操作后 #{event['seq']}（关联 #{decision}）：\n" + "\n".join(parts), changes


def compact_state_lines(state):
    lines = state_lines(state)
    if not state or not state.get("hud_present"):
        return lines
    fields = state["fields"]

    def value(key):
        number = fields[key]["value"]
        if number is None:
            return "?"
        return f"{number:,}" if key == "score" and isinstance(number, (int, float)) else str(number)

    main = [
        f"剩余 {value('turns_remaining')} 回合｜分数 {value('score')}",
        f"体力 {value('stamina')}｜元气 {value('genki')}",
        f"属性 {value('attribute')}｜倍率 {value('attribute_multiplier_percent')}%",
    ]
    extra = [line for line in lines[2:] if not line.startswith("Buff：")]
    buffs = [part for line in lines[2:] if line.startswith("Buff：") for part in line.removeprefix("Buff：").split("｜")]
    buff_rows = ["Buff：" + "｜".join(buffs[index : index + 2]) for index in range(0, len(buffs), 2)]
    return [*main, *extra, *buff_rows]


def operation_console(number, before_event, before_result, after_event=None, after_result=None):
    before_result = before_result or {}
    after_result = after_result or {}
    before = before_result.get("battle_state")
    cards = before_result.get("cards", [])
    operation = before_event.get("operation")
    title = {"play_card": "出牌", "use_drink_task": "使用饮料", "skip_round": "跳过回合", "move_cards": "移动卡牌"}.get(operation, "操作后观察")
    selected = next((card for card in cards if card["box"] == before_event.get("selected_box")), None)
    if operation == "play_card":
        title += "：" + (card_name(selected["identity"]) if selected else "牌名未识别")
    title = f"操作 {number}｜{title}"
    started, ended = before_event.get("monotonic_ns"), (after_event or {}).get("monotonic_ns")
    if isinstance(started, int) and isinstance(ended, int) and ended >= started:
        title += f"｜{(ended - started) / 1e9:.1f}秒"
    lines = [title]
    reason = {
        "game_recommendation": "游戏推荐",
        "only_available_card": "仅一张可用",
        "timeout_detector_confidence": "超时兜底",
        "existing_use_all_drinks": "使用现有饮料",
        "no_available_cards": "无可用牌",
        "no_cards_ocr": "无手牌",
    }.get(before_event.get("reason"), before_event.get("reason"))
    if reason:
        lines.append("选择依据：" + reason)
    if before_result.get("error"):
        lines.append("操作前识别失败：" + before_result["error"])
    elif before_event:
        lines.extend(["", "操作前：", *compact_state_lines(before)])
    else:
        lines.append("操作前：缺少前帧，无法比较变化")
    if operation in ("play_card", "skip_round", "move_cards"):
        lines.extend(["", "手牌：", hand_summary(cards, before_event.get("selected_box"))])
    changes = {}
    if after_event is None:
        lines[0] += "｜后续观察缺失"
        lines.append("后续观察：未采集到操作后画面，无法比较变化")
    elif after_result.get("error"):
        lines.append("操作后识别失败：" + after_result["error"])
    else:
        message, changes = after_console(after_event, before, after_result)
        if not after_result.get("battle_state", {}).get("hud_present"):
            lines.append("后续观察：未确认出牌 HUD")
        elif not before or not before.get("hud_present"):
            lines.extend(["", "操作后：", *compact_state_lines(after_result.get("battle_state"))])
        elif message:
            lines.extend(["", "观察变化：", *message.splitlines()[1:]])
        else:
            lines.append("观察变化：前后可读数值未变化；不代表没有其他效果")
    trace = [f"前 #{before_event['seq']}"] if before_event else []
    if after_event:
        trace.append(f"后 #{after_event['seq']}")
    lines.extend(["", "采集索引：" + " → ".join(trace)])
    return "\n".join(lines), changes


class OperationConsole:
    """配对操作前后画面用于展示，保留原始采集编号和动作标签。"""

    def __init__(self):
        self.count = 0
        self.pending = {}

    def start(self, event, result):
        self.count += 1
        self.pending[event["event_id"]] = (self.count, event, result)

    def finish(self, event, result):
        pending = self.pending.pop(event.get("decision_id"), None)
        if pending is None:
            self.count += 1
            pending = (self.count, {}, {})
        number, before_event, before_result = pending
        message, changes = operation_console(number, before_event, before_result, event, result)
        return message, changes, number

    def unfinished(self):
        pending, self.pending = self.pending, {}
        return [(operation_console(number, event, result)[0], event["event_id"], number) for number, event, result in pending.values()]


WAIT_REASONS = {
    "input_failed": "底层点击失败", "input_error": "点击结果异常，是否送达未确认",
    "different_candidate_selected": "仍选中其他候选", "highlight_unconfirmed": "选中高亮未确认",
    "layout_candidate_art_changed": "候选图标内容发生变化", "layout_mismatch": "候选位置不一致",
    "receive_button_missing": "领取按钮未识别", "detail_unstable": "详情仍在变化",
    "detail_refresh_unconfirmed": "详情刷新未确认", "restore_detail_mismatch": "原选择详情未恢复",
    "task_stopping": "任务手动停止", "observation_error": "截图或识别异常",
}


def preview_name(result):
    if not result or not (result.get("detail_verified") or result.get("identity_verified")):
        return "牌名未确认"
    identity = result.get("identity") or {}
    canonical = identity.get("database_name") if identity.get("status") == "matched" else None
    return canonical or result.get("observed_name") or "牌名未确认"


class RewardConsole:
    def __init__(self):
        self.count = 0
        self.pending = {}

    def observe(self, event, result=None):
        kind = event["kind"]
        if kind == "reward_page_start":
            self.count += 1
            self.pending[event["event_id"]] = {"number": self.count, "start": event, "previews": {}, "end": None,
                                                "restore": None, "receive": None, "retries": []}
            return None
        page_id = event.get("reward_page_id")
        page = self.pending.get(page_id)
        if not page:
            return None
        if kind == "reward_candidate_preview":
            page["previews"][event.get("candidate", {}).get("candidate_id")] = result or {}
        elif kind == "reward_selection_restored":
            page["restore"] = event
        elif kind == "reward_page_end":
            page["end"] = event
            if not event.get("safe_to_receive"):
                return self.finish(page_id, event["event_id"])
        elif kind == "reward_receive_input":
            page["receive"] = event
            return self.finish(page_id, event["event_id"])
        elif kind == "reward_preview_retry":
            page["retries"].append(event)
        return None

    def finish(self, page_id, event_id=None):
        page = self.pending.pop(page_id)
        start, end, receive = page["start"], page["end"] or {}, page["receive"] or {}
        selected = receive.get("selected_candidate_id") or start.get("selected_candidate_id")
        selected_name = preview_name(page["previews"].get(selected))
        policy = policy_name(start.get("policy_choice"))
        success = receive.get("framework_action_succeeded")
        status = "点击成功" if success is True else "点击失败" if success is False else "结果未确认" if receive else "未记录点击"
        candidates = sorted(start.get("layout", {}).get("candidates", []), key=lambda item: item["box"][0])
        cached_count = sum((result.get("reading") or {}).get("method") == "icon_cache" for result in page["previews"].values())
        lines = [f"奖励选择 {page['number']}｜{selected_name}｜{policy}",
                 f"领取按钮：{status}｜候选识别 {len(page['previews'])}/{len(candidates)}｜图标缓存 {cached_count} 项",
                 "", "候选（从左到右）："]
        for number, candidate in enumerate(candidates, 1):
            candidate_id = candidate["candidate_id"]
            result = page["previews"].get(candidate_id) or {}
            marker = "→ " if candidate_id == selected else ""
            lines.append(f"{number}｜{marker}{preview_name(result)}")
            effects = (result.get("effects") or {}).get("visible_text") or []
            lines.extend(f"  {text}" for text in effects)
            if not effects:
                defaults = catalog_lines(result.get("identity") or {})
                if defaults:
                    lines.append("  牌库默认：" + defaults[0])
                    lines.extend(f"  {text}" for text in defaults[1:])
                if not defaults:
                    lines.append("  效果：未确认")
            elif (result.get("reading") or {}).get("fallback_reason"):
                lines.append("  详情已补充识别；牌库与当前效果需核对")
            lines.append("")
        restored = (page["restore"] or {}).get("verified")
        unchanged = (page["restore"] or {}).get("method") == "original_selection_unchanged"
        lines.append("原选择：" + ("保持并核对" if unchanged and restored else "恢复已核对" if restored else "未确认"))
        defaults = sum((result.get("effects") or {}).get("semantic_status") == "catalog_default" for result in page["previews"].values())
        if defaults:
            lines.append(f"牌库默认效果：{defaults} 项｜定制及临时变化未确认")
        text_only = sum((result.get("effects") or {}).get("semantic_status") == "ocr_text_only"
                        for result in page["previews"].values())
        if text_only:
            lines.append(f"正文留存待核对：{text_only} 项｜未转换为结构化效果")
        if not end.get("candidate_previews_complete"):
            lines.append("候选采集不完整；未读取的候选保留未知")
            diagnostic = end.get("failure_diagnostic") or {}
            if diagnostic:
                target = diagnostic.get("candidate_id", "?").removeprefix("reward_")
                reason = diagnostic.get("reason")
                lines.append(f"候选 {target}：{WAIT_REASONS.get(reason, reason or '原因未确认')}｜尝试 {diagnostic.get('attempt', '?')} 次")
        if page["retries"]:
            lines.append(f"预览重试：{len(page['retries'])} 次｜失败截图与诊断已保存")
        if not receive:
            lines.append("未记录领取点击；不推断已获得奖励")
        lines.append(f"采集索引：#{start.get('seq', '?')}｜画面原文与牌库默认分开保存")
        return {"message": "\n".join(lines), "event_id": event_id or start["event_id"],
                "reward_page_id": page_id, "console_reward_number": page["number"], "scope": "reward"}

    def unfinished(self):
        return [self.finish(page_id) for page_id in list(self.pending)]
