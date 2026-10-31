"""统一整理并按顺序展示出牌、奖励和结算摘要。"""

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
        self.final_audition = None

    def observe(self, event, observation):
        self.frames += 1
        if observation.get("final_audition"):
            self.final_audition = observation["final_audition"]
            self.ending_observed = True
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
        if self.final_audition:
            result = self.final_audition
            outcome = "不合格" if result["outcome"] == "failed" else "合格"
            lines[0] = f"{scenario} {difficulty}｜最终试镜{outcome}"
            lines.insert(1, f"试镜得分：{show(result['score']['value'])} Pt｜名次 {show(result['place']['value'])}")
            lines.insert(2, f"最终培育评价：{show(observed)}｜评级 {show(check['observed_rank'])}")
        return {"message": "\n".join(lines), "scope": "settlement", "event_id": event_id, "evaluation_check": check}


def state_lines(state):
    if not state or not state.get("hud_present"):
        return ["状态：当前画面未确认出牌 HUD"]
    fields = state["fields"]

    def value(key):
        return fields[key]["value"] if fields[key]["value"] is not None else "?"

    lines = [f"状态：剩 {value('turns_remaining')} 回合｜分 {value('score')}｜体 {value('stamina')}｜元 {value('genki')}"]
    if state.get("layout") == "lesson":
        lines = [f"状态：剩 {value('turns_remaining')} 回合｜体 {value('stamina')}｜元 {value('genki')}"]
    else:
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
        lines.append("Buff：" + "｜".join(descriptions[index: index + 3]))
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
        return "操作后：当前画面未确认出牌 HUD", changes
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
    parts.extend("｜".join(remaining[index: index + 2]) for index in range(0, len(remaining), 2))
    if not parts:
        return None, changes
    return "操作后：\n" + "\n".join(parts), changes


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
    offset = 2
    if state.get("layout") == "lesson":
        main = [f"剩余 {value('turns_remaining')} 回合", f"体力 {value('stamina')}｜元气 {value('genki')}"]
        offset = 1
    extra = [line for line in lines[offset:] if not line.startswith("Buff：")]
    buffs = [part for line in lines[offset:] if line.startswith("Buff：") for part in line.removeprefix("Buff：").split("｜")]
    buff_rows = ["Buff：" + "｜".join(buffs[index: index + 2]) for index in range(0, len(buffs), 2)]
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
        title = f"出牌#{number}[" + (card_name(selected["identity"]) if selected else "牌名未识别") + "]"
    title = f"战斗 - {title}" if operation == "play_card" else f"战斗 - {title}#{number}"
    if operation == "play_card":
        elapsed = (after_event or {}).get("selection_click_seconds")
        if isinstance(elapsed, (int, float)) and elapsed >= 0:
            title += f" {round(elapsed, 1):g}s"
    elif operation == "skip_round":
        title += "（无手牌）" if before_event.get("reason") == "no_cards_ocr" else "（无可用牌）"
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
    elif after_event.get("observation_phase") == "next_decision_before" and not after_event.get("frame_reference"):
        lines.append("观察变化：没有下一次操作前画面，未知")
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


def preview_name(result):
    if not result or not (result.get("detail_verified") or result.get("identity_verified") or result.get("art_identity_verified")):
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
        start, receive = page["start"], page["receive"] or {}
        selected = receive.get("selected_candidate_id") or start.get("selected_candidate_id")
        selected_name = preview_name(page["previews"].get(selected))
        success = receive.get("framework_action_succeeded")
        status = "已点击领取" if success is True else "领取失败" if success is False else "领取未确认"
        candidates = sorted(start.get("layout", {}).get("candidates", []), key=lambda item: item["box"][0])
        lines = [f"奖励选择 - {status}：{selected_name}"]
        for number, candidate in enumerate(candidates, 1):
            candidate_id = candidate["candidate_id"]
            result = page["previews"].get(candidate_id) or {}
            marker = "→ " if candidate_id == selected else ""
            known = (result.get("identity") or {}).get("status") == "matched"
            suffix = ("｜识别异常" if result.get("error") else "" if known else
                      "｜强化未核验" if result.get("art_identity_verified") else "｜未匹配")
            lines.append(f"{number}｜{marker}{preview_name(result)}" + suffix)
        readings = [result.get("reading") or {} for result in page["previews"].values()]
        matched = sum(reading.get("method") == "icon_cache" for reading in readings)
        stored = sum(reading.get("cache_write") == "stored" for reading in readings)
        ocr = sum(reading.get("method") == "name_then_catalog" for reading in readings)
        lines.append(f"模板匹配：{matched} 项｜OCR 匹配：{ocr} 项｜新增缓存：{stored} 项")
        return {"message": "\n".join(lines), "event_id": event_id or start["event_id"],
                "reward_page_id": page_id, "console_reward_number": page["number"], "scope": "reward"}

    def unfinished(self):
        return [self.finish(page_id) for page_id in list(self.pending)]


def format_icon_matches(operation, matches):
    """只列出识别名称，整块保持单行且默认折叠，不加载图片。"""
    from html import escape

    name = {"shop": "商店", "strengthen": "强化", "delete_copy_exchange": "删除／复制／变换",
            "move_cards": "移动"}.get(operation, "页面")
    rows = [escape(match["database_name"]) for match in matches]
    if not rows:
        rows = ["未命中缓存中的已知图标；不代表没有可选项。"]
    return (f'<details><summary>{name}｜图标命中 {len(matches)} 项</summary><p>'
            + '<br/>'.join(rows) + '<br/>仅识别图标本体，强化、可选状态及实际获得情况未核验。</p></details>')


def format_operation(title, rows):
    """MFA 日志使用单个折叠根元素；正文用换行，避免多个块被横向排列。"""
    from html import escape

    body = "<br/>".join(escape(str(row)) for row in rows if str(row).strip())
    return (f"<details><summary>{escape(title)}</summary><p>{body}</p></details>"
            if body else escape(title))


def operation_detail_rows(details):
    """展示字段逐项展开；保留原始结构化记录，不把坐标数组直接堆在正文中。"""
    names = {"health": "体力", "points": "P 点", "vote": "票数", "Vo": "声乐", "Da": "舞蹈", "Vi": "形象",
             "target_threshold": "目标门槛", "candidates": "候选"}

    def display(value):
        if value is None:
            return "未知"
        if isinstance(value, bool):
            return "是" if value else "否"
        if isinstance(value, dict) and "status" in value and "value" in value:
            return display(value["value"])
        if isinstance(value, dict) and "current" in value and "max" in value:
            return f"{display(value['current'])}/{display(value['max'])}"
        if isinstance(value, (list, tuple)) and all(isinstance(v, (int, float)) for v in value):
            if len(value) == 2:
                return f"x={value[0]}, y={value[1]}"
            if len(value) == 4:
                return f"区域 x={value[0]}, y={value[1]}, 宽={value[2]}, 高={value[3]}"
        return str(value)

    rows = []

    def append(label, value):
        if label in {"actual_point", "framework_action_succeeded", "box", "roi"}:
            return
        if isinstance(value, dict) and not ({"status", "value"} <= value.keys() or {"current", "max"} <= value.keys()):
            for key, item in value.items():
                append(names.get(key, key), item)
            if not value:
                rows.append(f"{label}：无")
        elif isinstance(value, (tuple, list)) and any(isinstance(item, dict) for item in value):
            for index, item in enumerate(value, 1):
                append(f"{label} {index}", item)
        else:
            rows.append(f"{label}：{display(value)}")

    for key, value in details.items():
        append(names.get(key, key), value)
    return rows


def route_detail_rows(health, attributes, points, candidates, selected):
    """路线状态分短行，候选逐项编号；箭头表示本次决策选中项。"""
    rows = operation_detail_rows({"health": health, "points": points})
    rows = ["｜".join(rows)]
    rows.extend(operation_detail_rows({"attributes": attributes}))
    rows.append("可选事件：")
    names = {"Vo": "声乐", "Da": "舞蹈", "Vi": "形象"}
    for number, candidate in enumerate(candidates, 1):
        for name in candidate:
            if name == "SP":
                continue
            key = name + ("_SP" if candidate.get("SP") else "")
            marker = "→ " if key == selected.get("name") else ""
            label = names.get(name, name) + (" SP课程" if candidate.get("SP") else "")
            rows.append(f"{number}｜{marker}{label}")
    return rows


def log_operation(event, action, details):
    """已有 Custom 动作的同步摘要；不依赖截图、写盘或后台识别。"""
    from utils import logger

    logger.info(format_operation(f"{event} - {action}", operation_detail_rows(details)))
