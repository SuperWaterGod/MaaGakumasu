"""通过页面标签和 OCR 读取最终培育结算，不为评级逐个建立模板。"""

import re
import unicodedata

FIELDS = ("score", "rank", "Vo", "Da", "Vi")
LABELS = {
    "score": r"(?:プロデュース評価(?:値|点)?|育成評価(?:値|点)?|最終評価(?:値|点)?|評価値|評価点|総合評価|最终评价(?:分数|分|点)?|最終評價(?:分數|分|點)?|培育评[价分]|培育評[價分]|最终得分|最終得点|总得分|總得分)",
    "rank": r"(?:プロデュース評価|育成評価|評価ランク|最終評価|総合評価|最终评级|最終評級|评价等级|評價等級|最终评价|最終評價|培育评价|培育評價)",
    "Vo": r"(?:ボーカル|Vocal|Vo\.?|声乐|聲樂)",
    "Da": r"(?:ダンス|Dance|Da\.?|舞蹈)",
    "Vi": r"(?:ビジュアル|Visual|Vi\.?|形象)",
}
PAGE_LABEL = re.compile(
    r"プロデュース(?:結果|評価)|育成結果|最終(?:評価|パラメータ)|培育(?:结果|結果|评价|評價)|最终(?:评价|评级|属性)|最終(?:評價|評級|屬性)",
    re.IGNORECASE,
)
RANK = re.compile(r"(?:F|[ABC]\+?|S{1,3}\+?|S4\+?|S5)")
RANK_GLYPHS = re.compile(r"[SABCF45+]+")
# 三维属性通过麦克风、鞋和调色盘图标定位，
# 不使用评级字母推断属性。
PARAMETER_ICONS = {
    "Vo": {"roi": [145, 665, 65, 68]},
    "Da": {"roi": [280, 665, 65, 68]},
    "Vi": {"roi": [430, 665, 65, 68]},
}
for _field, _definition in PARAMETER_ICONS.items():
    _definition.update(template=f"produce/settlement/{_field}.png", threshold=0.94, method=10001)

# 公式来自与卡库对应的社区计算器，
# 许可说明见 assets/data/GAKUMAS_DATA_LICENSE.txt，
# 计算值只用于校验，不能替代未观察到的游戏结果。
FORMULA_SOURCE = {
    "repository": "https://github.com/gakumas-tools/gakumas-tools",
    "path": "gakumas-tools/utils/nia.js",
    "rank_path": "gakumas-tools/utils/produceRank.js",
    "kind": "community_calculator",
}
RANK_THRESHOLDS = (("S5", 35000), ("S4+", 30000), ("S4", 26000), ("SSS+", 23000), ("SSS", 20000),
                   ("SS+", 18000), ("SS", 16000), ("S+", 14500), ("S", 13000), ("A+", 11500),
                   ("A", 10000), ("B+", 8000), ("B", 6000), ("C+", 4500), ("C", 3000), ("F", 0))
# 各项依次为投票下限、固定贡献，以及以千分之一为单位的投票系数。
VOTE_RATING = ((140000, 5200, 30), (120000, 3800, 40), (100000, 2600, 50), (80001, 1600, 60),
               (60001, 1200, 65), (40001, 900, 70), (20001, 300, 85))


def nia_evaluation(parameters, votes):
    """使用试镜后的最终三维和投票数计算评价，不重复增加属性奖励。"""
    if len(parameters) != 3 or any(type(value) is not int or value < 0 for value in (*parameters, votes)):
        raise ValueError("Final parameters and votes must be nonnegative integers")
    parameter_rating = sum(parameters) * 23 // 10
    vote_rating = next((base + votes * rate // 1000 for bound, base, rate in VOTE_RATING if votes >= bound), 0)
    score = parameter_rating + vote_rating
    rank = next((rank for rank, threshold in RANK_THRESHOLDS if score >= threshold), None)
    return {"score": score, "rank": rank, "parameter_rating": parameter_rating, "vote_rating": vote_rating}


def hajime_evaluation(parameters, exam_score, place, difficulty):
    """计算初 REGULAR/PRO/MASTER 评价，输入三维已包含名次属性奖励。"""
    if difficulty not in ("REGULAR", "PRO", "MASTER"):
        raise ValueError("Unsupported Hajime difficulty")
    if len(parameters) != 3 or any(type(value) is not int or value < 0 for value in (*parameters, exam_score)):
        raise ValueError("Final parameters and exam score must be nonnegative integers")
    if type(place) is not int or place < 1:
        raise ValueError("Final exam placement must be positive")
    parameter_rating = sum(parameters) * 23 // 10
    placement_rating = {1: 1700, 2: 900, 3: 500}.get(place, 0)
    # 先按百分之一整数累加，避免分段换算边界的浮点误差。
    exam_hundredths = sum(max(0, min(exam_score, upper) - lower) * rate
                          for lower, upper, rate in ((0, 5000, 30), (5000, 10000, 15), (10000, 20000, 8),
                                                     (20000, 30000, 4), (30000, 40000, 2), (40000, exam_score, 1)))
    exam_rating = exam_hundredths // 100
    score = parameter_rating + placement_rating + exam_rating
    return {"score": score, "rank": next((rank for rank, bound in RANK_THRESHOLDS if score >= bound), None),
            "parameter_rating": parameter_rating, "placement_rating": placement_rating, "exam_rating": exam_rating}


def evaluation_check(scenario, final_settlement, fan_votes, final_exam=None, difficulty=None):
    inputs = {key: final_settlement[key]["value"] for key in ("Vo", "Da", "Vi")}
    final_exam = final_exam or {}
    difficulty = difficulty[0] if isinstance(difficulty, list) and len(difficulty) == 1 else difficulty
    if scenario == "NIA":
        inputs["fan_votes"] = fan_votes.get("value")
    elif scenario == "初":
        inputs.update(exam_score=(final_exam.get("score") or {}).get("value"),
                      place=(final_exam.get("place") or {}).get("value"))
    result = {"status": "unavailable", "source": FORMULA_SOURCE, "inputs": inputs,
              "calculated": None, "observed_score": final_settlement["score"]["value"],
              "observed_rank": final_settlement["rank"]["value"], "game_action_confirmed": None}
    if scenario not in ("NIA", "初") or (scenario == "初" and difficulty not in ("REGULAR", "PRO", "MASTER")):
        result["reason"] = "unsupported_scenario_or_difficulty"
        return result
    if scenario == "初":
        result["source"] = {**FORMULA_SOURCE, "path": FORMULA_SOURCE["rank_path"]}
    if any(value is None for value in inputs.values()):
        result["reason"] = "missing_formula_inputs"
        return result
    parameters = [inputs[key] for key in ("Vo", "Da", "Vi")]
    try:
        calculated = (nia_evaluation(parameters, inputs["fan_votes"]) if scenario == "NIA" else
                      hajime_evaluation(parameters, inputs["exam_score"], inputs["place"], difficulty))
    except (TypeError, ValueError):
        result["reason"] = "invalid_formula_inputs"
        return result
    result["calculated"] = calculated
    checks = [result[key] == calculated[field] for key, field in (("observed_score", "score"), ("observed_rank", "rank"))
              if result[key] is not None and calculated[field] is not None]
    result["status"] = ("match" if all(checks) else "mismatch") if checks else "calculated_only"
    return result


def text_value(text):
    return unicodedata.normalize("NFKC", text).strip()


def unknown(reason="missing_or_ambiguous_value", evidence=None):
    return {"value": None, "status": "unknown", "reason": reason, "evidence": evidence or []}


def parsed_value(text, field):
    text = re.sub(r"\s+", "", text_value(text)).strip(":：")
    if field == "rank":
        text = text.upper()
        return text if RANK.fullmatch(text) else None
    if not re.fullmatch(r"(?:[0-9]{1,7}|[0-9]{1,3}(?:,[0-9]{3}){1,2})", text):
        return None
    value = int(text.replace(",", ""))
    return value if 0 <= value <= (9999999 if field == "score" else 99999) else None


def eligible(tokens):
    return [item for item in tokens if item.get("confidence", 0) >= 0.8 and len(item.get("box", [])) == 4]


def label_match(token, field):
    return re.fullmatch(LABELS[field] + r"\s*[:：]?\s*(.*?)", text_value(token["text"]), re.IGNORECASE)


def combine_rank(tokens):
    """只在单个独立标签区域内拼接拆开的 SS、S+ 等评级字符。"""
    parts = sorted(eligible(tokens), key=lambda item: item["box"][0])
    parts = [item for item in parts if RANK_GLYPHS.fullmatch(re.sub(r"\s+", "", text_value(item["text"])).upper())]
    if not parts:
        return None, []
    for left, right in zip(parts, parts[1:]):
        x, y, w, h = left["box"]
        rx, ry, rw, rh = right["box"]
        if rx - (x + w) > max(24, min(h, rh) * 0.65) or abs((y + h / 2) - (ry + rh / 2)) > max(h, rh) * 0.55:
            return None, parts
    return parsed_value("".join(item["text"] for item in parts), "rank"), parts


def label_region(label, field, tokens):
    """按相邻标签和下一段文字限定标签下方的读取区域。"""
    x, y, w, h = label["box"]
    same_row = [item for item in tokens if item is not label and any(label_match(item, key) for key in FIELDS)
                and abs(item["box"][1] - y) <= max(h, item["box"][3])]
    left = max(0, x - (24 if field in ("Vo", "Da", "Vi") else 12))
    right = min(720, x + max(w + 24, 140 if field in ("Vo", "Da", "Vi") else 240))
    for other in same_row:
        ox, _, ow, _ = other["box"]
        if ox >= x + w:
            right = min(right, (x + w + ox) // 2)
        elif ox + ow <= x:
            left = max(left, (ox + ow + x) // 2)
    top = y + h
    bottom = min(1100, top + (290 if field == "rank" else 150))
    for other in tokens:
        ox, oy, ow, oh = other["box"]
        # 粉丝、奖励等新文字行作为数字区域的下边界。
        if oy > top + 20 and ox < right and ox + ow > left and re.search(r"[ぁ-ヿ一-鿿]", other["text"]):
            bottom = min(bottom, oy)
    return [int(left), int(top), int(max(1, right - left)), int(max(1, bottom - top))]


def inside(token, roi):
    x, y, w, h = token["box"]
    left, top, width, height = roi
    return left <= x + w / 2 <= left + width and top <= y + h / 2 <= top + height


def merge_readings(readings):
    if any(reading.get("uncertain_rank_glyph") for reading in readings):
        return unknown("uncertain_rank_glyph", readings)
    accepted = [reading for reading in readings if reading.get("value") is not None]
    values = {reading["value"] for reading in accepted}
    if len(values) == 1:
        return {"value": next(iter(values)), "status": "observed", "evidence": accepted}
    return unknown("conflicting_ocr_views" if values else "missing_or_ambiguous_value", readings)


def uncertain_rank_glyph(items):
    return any(item.get("confidence", 0) < 0.8 and
               RANK_GLYPHS.fullmatch(re.sub(r"\s+", "", text_value(item["text"])).upper()) for item in items)


def region_reading(tokens, roi, field, read_ocr=None, parse_number=parsed_value):
    views = [[item for item in tokens if inside(item, roi)]]
    if read_ocr:
        views.append(read_ocr(roi))
    readings = []
    for items in views:
        if field == "rank":
            value, _ = combine_rank(items)
        else:
            numbers = [parse_number(item["text"], field) for item in eligible(items)]
            numbers = [value for value in numbers if value is not None]
            value = numbers[0] if len(numbers) == 1 else None
        readings.append({"value": value, "roi": roi, "tokens": items, "method": "verified_layout_roi",
                         "uncertain_rank_glyph": field == "rank" and uncertain_rank_glyph(items)})
    return merge_readings(readings)


def final_evaluation_panel(tokens, read_ocr=None):
    headings = [item for item in eligible(tokens) if
                re.fullmatch(r"最終プロデュース評価|最终培育评价|最終培育評價", text_value(item["text"]))
                and 80 <= item["box"][1] <= 270]
    if len(headings) != 1 or not any(re.search(r"TAP.*(?:次|下)|点击.*继续|點擊.*繼續", text_value(item["text"]), re.I)
                                     and item["box"][1] >= 1050 for item in eligible(tokens)):
        return None
    result = {key: unknown("not_on_evaluation_panel") for key in FIELDS}
    result["score"] = region_reading(tokens, [175, 800, 370, 115], "score", read_ocr)
    # 评分出现前的评级可能仍在动画中，
    # 不将途中的 S 与完成后的 S+ 当作冲突结果。
    if result["score"]["value"] is None:
        result["rank"] = unknown("evaluation_animation_incomplete", headings)
    else:
        result["rank"] = region_reading(tokens, [165, 310, 390, 420], "rank", read_ocr)
    return result


def parameter_panel(tokens, read_ocr, match_templates):
    # 只有出现疑似三维数字行时，才尝试匹配三个小属性图标。
    if not read_ocr or not match_templates or not any(
        650 <= item["box"][1] <= 735 and re.fullmatch(r"[0-9][0-9,\s]+", text_value(item["text"])) for item in eligible(tokens)
    ):
        return None
    hits = {}
    for key, definition in PARAMETER_ICONS.items():
        matches = match_templates(definition)
        if len(matches) != 1 or matches[0]["score"] < definition["threshold"]:
            return None
        hits[key] = matches[0]
    if max(hit["box"][1] for hit in hits.values()) - min(hit["box"][1] for hit in hits.values()) > 5:
        return None
    result = {key: unknown("not_on_parameter_panel") for key in FIELDS}
    for key, hit in hits.items():
        x, y, w, h = hit["box"]
        roi = [x + w - 2, y - 4, 110 if key != "Vo" else 100, h + 8]
        result[key] = region_reading([], roi, key, read_ocr)
        result[key].update(attribute_icon=hit, icon_template=PARAMETER_ICONS[key]["template"])
    return result


def exam_value(text, field):
    suffix = r"(?:Pt|点|分)?" if field == "score" else r"位"
    match = re.fullmatch(r"([0-9]{1,7}|[0-9]{1,3}(?:,[0-9]{3}){1,2})\s*" + suffix, text_value(text), re.I)
    if not match:
        return None
    value = int(match[1].replace(",", ""))
    return value if field == "score" or 1 <= value <= 99 else None


def completion_panel(tokens, read_ocr):
    """初的完成卡中，评级和最终考试名次位于各自标签上方。"""
    if not read_ocr:
        return None
    tokens = eligible(tokens)
    exams = [item for item in tokens if re.fullmatch(r"最終試験|最终考试|最終考試", text_value(item["text"]))
             and 940 <= item["box"][1] <= 1060 and item["box"][0] < 300]
    grades = [item for item in tokens if re.fullmatch(r"プロデュース評価|培育评价|培育評價", text_value(item["text"]))
              and item["box"][0] >= 360]
    if len(exams) != 1 or len(grades) != 1 or abs(exams[0]["box"][1] - grades[0]["box"][1]) > 20:
        return None
    if not any(re.fullmatch(r"完了する|完成|结束|結束", text_value(item["text"])) and item["box"][1] >= 1080 for item in tokens):
        return None
    x, y, w, _ = exams[0]["box"]
    place_roi = [int(x + w / 2 - 65), y - 241, 145, 165]
    x, y, w, _ = grades[0]["box"]
    grade_roi = [int(x + w / 2 - 125), y - 270, 250, 260]
    # 全屏 OCR 可能只读出 A 并漏掉独立的加号，
    # 因此在已核验的完成卡布局内读取完整评级区域。
    return {"rank": region_reading([], grade_roi, "rank", read_ocr),
            "place": region_reading([], place_roi, "place", read_ocr, exam_value)}


def final_exam_observation(tokens, read_ocr=None):
    """只读取明确标注的最终考试结果，不将最后一节课程当作考试。"""
    tokens = eligible(tokens)
    result = {key: unknown("final_exam_label_missing") for key in ("score", "place")}
    anchors = [item for item in tokens if re.fullmatch(r"最終試験(?:結果)?|最终考试(?:结果)?|最終考試(?:結果)?", text_value(item["text"]))]
    explicit = [item for item in tokens if re.match(r"(?:最終試験|最终考试|最終考試)(?:スコア|得点|得分|分数|順位|名次)", text_value(item["text"]))]
    if not anchors and not explicit:
        return result
    anchor = anchors[0] if len(anchors) == 1 else None
    for field, pattern in (("score", r"スコア|得点|得分|分数"), ("place", r"順位|名次")):
        readings = []
        for item in tokens:
            match = re.fullmatch(r"(?:(?:最終試験|最终考试|最終考試)\s*)?(?:" + pattern + r")\s*[:：]?\s*(.*)", text_value(item["text"]))
            if not match or (item not in explicit and anchor is None):
                continue
            if item not in explicit and anchor:
                ax, ay, aw, ah = anchor["box"]
                if not (ax - 30 <= item["box"][0] <= ax + aw + 30 and ay <= item["box"][1] <= ay + 450):
                    continue
            inline = exam_value(match[1], field)
            if inline is None and field == "place" and re.fullmatch(r"[1-9][0-9]?", match[1]):
                inline = int(match[1])
            if inline is not None:
                readings.append({"value": inline, "tokens": [item], "method": "explicit_final_exam_label"})
            else:
                x, y, w, h = item["box"]
                nearby = [other for other in tokens if x + w <= other["box"][0] <= x + w + 180
                          and abs(other["box"][1] - y) <= max(h, other["box"][3]) / 2]
                for other in nearby:
                    value = exam_value(other["text"], field)
                    if value is None and field == "place" and re.fullmatch(r"[1-9][0-9]?", text_value(other["text"])):
                        value = int(text_value(other["text"]))
                    if value is not None:
                        readings.append({"value": value, "tokens": [item, other], "method": "final_exam_same_row"})
                if field == "score":
                    labels = [other for other in tokens if exam_value(other["text"], "score") is None]
                    readings.append(region_reading(tokens, label_region(item, "score", labels), "score", read_ocr, exam_value))
        if field == "place" and anchor:
            x, y, w, h = anchor["box"]
            for item in tokens:
                match = re.fullmatch(r"(?:第)?([1-9][0-9]?)位", text_value(item["text"]))
                if match and x - 30 <= item["box"][0] <= x + w + 30 and y <= item["box"][1] <= y + 450:
                    readings.append({"value": int(match[1]), "tokens": [anchor, item], "method": "below_final_exam_heading"})
        result[field] = merge_readings(readings)
        if field == "place" and result[field]["value"] is not None and not 1 <= result[field]["value"] <= 99:
            result[field] = unknown("invalid_exam_placement", readings)
    return result


def read_fields(tokens, read_ocr=None):
    """读取明确的最终评价字段，考试分数与投票数分别处理。"""
    all_tokens = tokens
    tokens = eligible(tokens)
    panel = final_evaluation_panel(all_tokens, read_ocr)
    if panel is not None:
        return panel
    result = {field: unknown("final_evaluation_label_missing") for field in FIELDS}
    if not any(PAGE_LABEL.search(text_value(token["text"])) for token in tokens):
        return result
    for field in FIELDS:
        readings = []
        for label in tokens:
            match = label_match(label, field)
            if not match:
                continue
            inline = parsed_value(match[1], field)
            if inline is not None:
                readings.append({"value": inline, "method": "inline_label", "tokens": [label]})
                continue
            # 同行数值须位于标签右侧且唯一。
            x, y, w, h = label["box"]
            row = [item for item in tokens if item is not label and x + w <= item["box"][0] <= x + w + 160
                   and abs(item["box"][1] + item["box"][3] / 2 - y - h / 2) <= max(h, item["box"][3]) / 2]
            if field == "rank":
                value, evidence = combine_rank(row)
                if value is not None:
                    readings.append({"value": value, "method": "right_of_label", "tokens": [label, *evidence]})
            else:
                row_values = [(parsed_value(item["text"], field), item) for item in row]
                row_values = [(value, item) for value, item in row_values if value is not None]
                if len(row_values) == 1:
                    readings.append({"value": row_values[0][0], "method": "right_of_label", "tokens": [label, row_values[0][1]]})
            roi = label_region(label, field, tokens)
            views = [[item for item in all_tokens if inside(item, roi)]]
            if read_ocr:
                views.append(read_ocr(roi))
            for items in views:
                if field == "rank":
                    value, evidence = combine_rank(items)
                    readings.append({"value": value, "method": "below_label", "roi": roi,
                                     "tokens": [label, *items], "uncertain_rank_glyph": bool(uncertain_rank_glyph(items))})
                else:
                    numbers = [(parsed_value(item["text"], field), item) for item in eligible(items)]
                    numbers = [(value, item) for value, item in numbers if value is not None]
                    readings.append({"value": numbers[0][0] if len(numbers) == 1 else None,
                                     "method": "below_label", "roi": roi, "tokens": [label, *items]})
        result[field] = merge_readings(readings)
    return result


def ranking_observation(tokens):
    """排名页仅玩家行有日志按钮；按同一行读取分数和名次，不假定玩家位置。"""
    tokens = eligible(tokens)
    logs = [t for t in tokens if re.fullmatch(r"ログ|日志|日誌", text_value(t["text"]))
            and t["box"][0] > 550 and 250 < t["box"][1] < 1080]
    if len(logs) != 1:
        return None
    y = logs[0]["box"][1]
    scores = [t for t in tokens if 190 < t["box"][0] < 350 and abs(t["box"][1] - y) < 55
              and re.fullmatch(r"[\d,]+Pt", text_value(t["text"]), re.IGNORECASE)]
    ranks = [t for t in tokens if t["box"][0] < 150 and abs(t["box"][1] - y) < 70
             and re.fullmatch(r"[1-9](?:st|nd|rd|th)", text_value(t["text"]), re.IGNORECASE)]
    if len(scores) != 1 or len(ranks) != 1:
        return None
    return {"score": {"value": int(re.sub(r"\D", "", scores[0]["text"])), "status": "observed", "evidence": [logs[0], scores[0]]},
            "place": {"value": int(ranks[0]["text"][0]), "status": "observed", "evidence": [logs[0], ranks[0]]}}


class FinalExamTracker:
    """排名先出现、最终考试标签后出现；仅关联同一场结束窗口，拒绝跨战斗沿用。"""

    def __init__(self, scenario):
        self.scenario = scenario
        self.pending = None

    def observe(self, event, observation=None):
        if event.get("kind") in ("battle_invocation_start", "exam_failure_or_retry", "produce_gameplay_entry"):
            self.pending = None
        if self.scenario != "初" or observation is None:
            return
        ranking = observation.get("exam_ranking")
        if ranking:
            self.pending = {key: {**value, "source_event_id": event.get("event_id")} for key, value in ranking.items()}
        tokens = eligible(observation.get("raw_ocr", []))
        # 只接受左上角的最终考试页面标签，不用完成卡上的历史标签关联旧排名。
        confirmed = any(re.fullmatch(r"最終試験|最终考试|最終考試", text_value(t["text"]))
                        and t["box"][0] < 200 and t["box"][1] < 180 for t in tokens)
        if confirmed and self.pending:
            final = observation["final_exam"]
            for key, value in self.pending.items():
                final[key] = merge_readings([final[key], value])
            self.pending = None


def settlement_observation(tokens, read_ocr=None, match_templates=None):
    fields = read_fields(tokens, read_ocr)
    parameters = parameter_panel(tokens, read_ocr, match_templates)
    if parameters is not None:
        fields = parameters
    fan_votes = unknown("final_fan_vote_label_missing")
    if any(PAGE_LABEL.search(text_value(item["text"])) for item in eligible(tokens)):
        labels = [item for item in eligible(tokens) if re.fullmatch(r"ファン投票数|粉丝投票数|粉絲投票數", text_value(item["text"]))]
        if len(labels) == 1:
            fan_votes = region_reading(tokens, label_region(labels[0], "score", eligible(tokens)), "score", read_ocr)
    final_exam = final_exam_observation(tokens, read_ocr)
    completion = completion_panel(tokens, read_ocr)
    if completion is not None:
        fields["rank"] = completion["rank"]
        final_exam["place"] = merge_readings([final_exam["place"], completion["place"]])
    return {"schema_version": 2, "settlement": fields, "raw_ocr": tokens,
            "fan_votes": fan_votes,
            "final_exam": final_exam,
            "exam_ranking": ranking_observation(tokens),
            "evaluation_page_observed": any(value["value"] is not None for value in fields.values()),
            "settlement_fields_complete": all(value["value"] is not None for value in fields.values()),
            "game_action_confirmed": None}
