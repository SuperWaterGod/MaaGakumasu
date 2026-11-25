"""按流派评价当前画面，只返回一个可执行目标。"""

import re
import json
import math
import operator
from functools import lru_cache
from collections import Counter

from .selection import direct_actions

CARD_ROLES = {"extend", "recovery", "extra_score", "extra_impression", "cycle", "engine", "multiply", "threshold", "sustain",
              "output", "impression_score", "profitable_conversion", "impression", "direct_score", "economy", "drive", "guard", "support",
              "extra_drive", "persistent_score", "score_boost", "exchange", "free_setup", "register_effect", "active_guard_setup"}
HAND_TESTS = {"weak", "conversion", "active_conversion", "active", "upgradable", "needs_drive", "sustained_drive", "unlock_drive", "needs_extra_use"}
LIMITS = {"min_turns", "max_turns", "min_impression", "min_genki", "max_genki", "min_stamina", "max_stamina",
          "min_multiplier", "min_peak_percent", "min_useful_cards", "remaining_card_uses"}
COMPARISONS = {">=": operator.ge, "<=": operator.le, "==": operator.eq, ">": operator.gt, "<": operator.lt}


def load_battle_rules(path):
    config = json.loads(path.read_bytes())
    if config["version"] != 2:
        raise ValueError("无法读取此版本的战斗规则")
    effects = set()
    for profile in config["profiles"]:
        if profile["effect"] in effects or not set(profile["drink_layouts"]) <= set(profile["layouts"]) <= {"lesson", "exam"}:
            raise ValueError("重复的推荐效果或未知布局")
        effects.add(profile["effect"])
        roles = {"use_drink": set(profile["drink_effects"].values()), "play_card": CARD_ROLES}
        ids = set()
        for rule in profile["rules"]:
            if rule["role"] == "profitable_conversion":
                reserve = profile.get("future_conversion_percent")
                if type(reserve) is not int or reserve < 0:
                    raise ValueError("比较成长和转分需要配置后续转分预留倍率")
            if (rule["id"] in ids or not rule["reason"] or rule["role"] not in roles.get(rule["action"], ())
                    or rule.keys() - {"id", "action", "role", "reason", "hand", "next_card", "once_per_battle"} - LIMITS
                    or "once_per_battle" in rule and (rule["action"] != "use_drink" or type(rule["once_per_battle"]) is not bool)
                    or ("hand" in rule and rule["hand"] not in HAND_TESTS)
                    or ("next_card" in rule and (rule["action"] != "use_drink" or rule["next_card"] not in {"active_conversion", "drive_score"}))
                    or any(type(rule[k]) is not int or rule[k] < 0 for k in rule.keys() & LIMITS)
                    or rule.get("max_turns", float("inf")) < rule.get("min_turns", 1)):
                raise ValueError(f"无效战斗规则 {rule}")
            ids.add(rule["id"])
    return config


def choose_profile(config, song_id, effect, scenario, difficulty):
    """歌曲仅用于固有联动；自定义歌曲也按配置的推荐效果选流派。"""
    levels = {"初": ("REGULAR", "PRO", "MASTER"), "NIA": ("PRO", "MASTER")}
    if difficulty not in levels.get(scenario, ()):
        return None
    return next(({**p, "reserve_recovery": song_id in p["reserve_recovery_songs"]} for p in config["profiles"] if p["effect"] == effect), None)


@lru_cache(maxsize=2048)
def _statements(text):
    """保留 DSL 语句顺序和块边界，避免把未来触发或目标牌改造当成本次效果。"""
    root = []
    stack = [root]
    for part in re.findall(r"[^;{}]+[{}]?|[{}]", text):
        part = part.strip()
        if not part:
            continue
        opening, closing = part.endswith("{"), part.endswith("}")
        command = re.sub(r"^@\w+\s+", "", part.rstrip("{}").strip())
        body = []
        if command:
            stack[-1].append((command, body))
        if opening:
            stack.append(body)
        if closing and len(stack) > 1:
            stack.pop()
    return root


def _number(expr, values):
    """只读取当前已知的标量及数据库中的简单加乘式。"""
    total = 0
    for term in expr.split("+"):
        product = 1
        for factor in term.split("*"):
            factor = factor.strip()
            value = float(factor) if re.fullmatch(r"\d+(?:\.\d+)?", factor) else values.get(factor)
            if value is None:
                return None
            product *= value
        total += product
    return total


def _condition(expr, values):
    for clause in expr.split("&"):
        match = re.fullmatch(r"(\w+)\s*(>=|<=|==|>|<)\s*([\w.*]+)", clause.strip())
        if not match:
            return False
        left, right = values.get(match[1]), _number(match[3], values)
        if left is None or right is None or not COMPARISONS[match[2]](left, right):
            return False
    return True


def _item_affinity(items, identity, face, after, facts):
    """只比较已持有道具的可见触发条件，不将未知的触发次数折算成得分。"""
    tags = {"isDirectEffect": True}
    tags["cardHasEffect(goodImpressionTurns)"] = facts["goodImpressionTurns"] > 0
    tags["cardHasEffect(genki)"] = facts["genki"] > 0
    tags["cardHasEffect(fixedStamina)"] = "fixedStamina+=" in direct_actions(face.get("actions", ""))
    kind = identity.get("type") or face.get("type")
    phases = {"afterCardUsed", "cardUsed"}
    if kind in ("mental", "active"):
        phases.update({f"afterCardUsed[{kind}]", f"cardUsed[{kind}]", f"{kind}CardUsed"})
    if facts["goodImpressionTurns"] > 0:
        phases.add("goodImpressionTurnsIncreased")
    if facts["motivation"] > 0:
        phases.add("motivationIncreased")
    if any(word in face.get("cost", "") for word in ("motivation-=", "goodImpressionTurns-=")):
        phases.add("buffCostConsumed")
    matches = 0
    for phase, terms in items:
        if phase not in phases:
            continue
        # 缺少卡组计数、属性、固有牌版本或道具内部计数的条件保持未知。
        if all(tags.get(term, False) or _condition(term, after) for term in terms):
            matches += 1
    return matches


def _evaluate(steps, values, facts):
    for command, body in steps:
        if body:
            if command.startswith("if:") and _condition(command[3:], values):
                _evaluate(body, values, facts)
            elif command.startswith("at:"):
                _future(command[3:], body, values, facts)
            continue
        change = re.fullmatch(r"(\w+)(\+=|-=|\*=|/=|=)(.+)", command)
        if change:
            name, op, expr = change.groups()
            amount = _number(expr, values)
            if amount is None:
                # 即使基数缺失，也可确认“按好印象转分”这一用途。
                if name == "score" and expr.startswith("goodImpressionTurns"):
                    facts["conversion"] = 1
                base = re.fullmatch(r"(\d+)\+cardsUsed\*\d+", expr)
                if op == "+=" and base:
                    facts[name] += int(base[1])
                values[name] = None
                continue
            previous = values.get(name)
            if op == "=":
                values[name] = amount
                if previous is not None:
                    facts[name] += amount - previous
                continue
            if op in ("*=", "/="):
                if previous is None or (op == "/=" and not amount):
                    continue
                amount = int(previous * amount if op == "*=" else previous / amount) - previous
                if name == "goodImpressionTurns":
                    facts["multiply"] += amount
            elif op == "-=":
                amount = -amount
            elif name == "genki":
                amount += (values.get("motivation") or 0) * values.get("motivationMultiplier", 1)
            facts[name] += amount
            if name == "score" and expr.startswith("goodImpressionTurns"):
                facts["conversion"] += max(1, amount)
            if name == "score" and op == "+=" and re.fullmatch(r"motivation(?:\*[\d.]+)?", expr) and amount > 0:
                facts["drive_score"] = 1
            if previous is not None:
                values[name] = max(0, previous + amount)
        elif command.startswith(("drawCard", "exchangeHand", "upgradeHand", "moveRandomToHand", "addRandomUpgraded")):
            facts["support"] += 1
        elif match := re.fullmatch(r"setGoodImpressionTurns(Buff|EffectBuff|TimesBuff)\(([\d.]+)(?:,\s*(\d+))?\)", command):
            duration = min(values["turnsRemaining"], int(match[3]) if match[3] else values["turnsRemaining"])
            factor = float(match[2])
            if match[1] == "Buff":
                facts["engine"] += factor * duration
            elif values.get("goodImpressionTurns"):
                # 回合末效果倍率和发动次数只作用于回合末，不乘到卡牌主动转分上。
                passive = values["goodImpressionTurns"] * factor
                facts["engine"] += passive * duration
                facts["passive_score"] += passive * min(duration, 2)
        elif command.startswith("setScoreBuff("):
            facts["score_buff"] += 1


def _future(phase, body, values, facts, horizon=None):
    """给持续效果独立的建立牌优先级；不将未来的抽牌和条件得分加入即时收益。"""
    delay = next((int(cmd[6:]) for cmd, _ in body if re.fullmatch(r"delay:\d+", cmd)), 0)
    future = (horizon if horizon is not None else values["turnsRemaining"]) - delay - (horizon is None and phase != "endOfTurn")
    if future <= 0:
        return
    limit = next((int(cmd[6:]) for cmd, _ in body if re.fullmatch(r"limit:\d+", cmd)), future)
    for cmd, nested in body:
        if nested:
            if cmd.startswith("if:") and _condition(cmd[3:], values):
                _future(phase, nested, values, facts, min(limit, future))
            elif phase == "cardUsed[active]" and cmd.startswith("if:effectCounter%") and any(c.startswith("genki+=") for c, _ in nested):
                facts["active_guard_setup"] = 1
            elif phase in ("cardUsed", "afterCardUsed") and cmd == "if:cardHasEffect(goodImpressionTurns)" and values.get("goodImpressionTurns"):
                if any(c.startswith("score+=goodImpressionTurns") for c, _ in nested):
                    facts["engine"] += min(limit, future)
            elif (phase == "cardUsed" and future >= 3 and cmd.startswith("if:cardHasEffect(goodImpressionTurns) & ")
                  and _condition(cmd.split(" & ", 1)[1], values)
                  and any(c.startswith("if:effectCounter%") and any(b.startswith("setGoodImpressionTurnsTimesBuff(") for b, _ in children)
                          for c, children in nested)):
                # 只有持续登记的建立牌价值，不假定后续已打足三张牌。
                facts["engine"] += 1
            continue
        if re.fullmatch(r"goodImpressionTurns\+=\d+", cmd):
            facts["engine"] += int(cmd.split("=")[1]) * min(limit, future)
        elif cmd.startswith(("score+=goodImpressionTurns", "goodImpressionTurns*=", "setGoodImpressionTurns")):
            facts["engine"] += min(limit, future)
        elif cmd.startswith(("score+=genki", "score+=motivation")):
            points = _number(cmd.split("=")[1], values)
            if points:
                facts["persistent_score"] += points * min(limit, future)
        elif cmd.startswith(("drawCard", "upgradeHand")):
            facts["support"] += 1
        elif phase in ("turn", "startOfTurn", "endOfTurn") and re.fullmatch(r"genki\+=\d+", cmd):
            facts["future_guard"] += int(cmd.split("=")[1]) * min(limit, future)


def _card_roles(identity, state_values, profile, item_thresholds, item_triggers=()):
    if identity.get("status") != "matched" or not identity.get("card_id"):
        return None
    face = identity.get("effective_effect_dsl") or identity.get("reference_effect_dsl")
    if not face:
        return None
    values = state_values.copy()
    facts = Counter()
    cost = face.get("cost", "")
    actions = direct_actions(face.get("actions", ""))
    if re.search(r"\b(?:useSelectedFree|moveSelectedToHand|holdSelected)\[", actions):
        # 本次选择需要二级选牌，留给已有推荐流程。
        return {}
    gate = re.fullmatch(r"(?:@usable )?if:(\w+)(>=|<=|==)(\d+)", face.get("conditions", ""))
    if gate and values.get(gate[1]) is not None and not COMPARISONS[gate[2]](values[gate[1]], int(gate[3])):
        return {}
    for name, n in re.findall(r"(\w+)-=(\d+)", cost):
        amount = int(n)
        if name in ("cost", "stamina"):
            factor = (.5 if values.get("halfCostTurns") else 1) * (2 if values.get("doubleCostTurns") else 1)
            amount = 0 if values.get("nullifyCostCards") else math.ceil(amount * factor)
        if name == "cost" and values.get("genki") is not None:
            absorbed = min(values["genki"], amount)
            values["genki"] -= absorbed
            amount -= absorbed
            name = "stamina"
        if values.get(name) is not None:
            values[name] -= amount
            if values[name] < 0 or name == "stamina" and values[name] == 0:
                return {}
    _evaluate(_statements(face.get("actions", "")), values, facts)
    growth_cost = re.search(r"goodImpressionTurns-=(\d+)", cost)
    growth = facts["goodImpressionTurns"] - (int(growth_cost[1]) if growth_cost else 0)
    has_extra = facts["cardUsesRemaining"] > 0
    if profile["reserve_recovery"] and re.search(r"\bfixedStamina\+=", actions):
        return {"recovery": max(1, growth) if has_extra else 0}
    score = facts["score"]
    heal = facts["fixedStamina"] + facts["stamina"]
    guard = facts["genki"] + facts["fixedGenki"]
    impression = state_values.get("goodImpressionTurns")
    roles = {"impression": growth, "direct_score": score, "impression_score": facts["conversion"], "extra_score": facts["conversion"] if has_extra else 0,
             "single_use": face.get("limit") == 1,
             "returns_use": has_extra, "drive_score": facts["drive_score"],
             "active": identity.get("type", face.get("type")) == "active", "active_guard_setup": facts["active_guard_setup"],
             "extra_impression": growth if has_extra else 0, "multiply": facts["multiply"], "engine": facts["engine"],
             "extend": facts["turnsRemaining"] if has_extra else 0, "cycle": facts["support"] if has_extra and "exchangeHand" not in actions else 0,
             "drive": facts["motivation"], "guard": guard + facts["future_guard"], "sustain": heal + guard, "support": facts["support"],
             "economy": facts["halfCostTurns"] + facts["costReduction"] + facts["nullifyCostCards"],
             "output": score + facts["passive_score"] + growth * min(state_values["turnsRemaining"], 2),
             "extra_drive": facts["motivation"] if has_extra else 0,
             "persistent_score": facts["persistent_score"], "score_boost": facts["score_buff"],
             "free_setup": int(has_extra and not growth_cost and not facts["support"] and
                               (facts["costReduction"] or facts["nullifyDebuff"] or facts["halfCostTurns"] or guard > 0)),
             # 正文登记的后续效果会随这张牌一同生效。只作低优先级铺垫，不预测随机牌或内部计数。
             "register_effect": int(has_extra and not growth_cost and any(cmd.startswith("at:") for cmd, _ in _statements(face.get("actions", "")))),
             "exchange": int("exchangeHand" in actions)}
    if impression is not None and any(impression < threshold <= impression + growth for threshold in item_thresholds):
        roles["threshold"] = growth
    if roles["output"] > 0 and (identity.get("upgraded") is False or face.get("upgraded") is False):
        roles["upgradable"] = 1
    if facts["conversion"] and identity.get("type", face.get("type")) == "active" and identity.get("rarity", face.get("rarity")) in ("N", "R", "SR", "SSR"):
        roles["active_conversion"] = 1
    if item_triggers:
        roles["affinity"] = _item_affinity(item_triggers, identity, face, values, facts)
    return roles


def battle_item_conditions(selection):
    """只取实际持有的战斗道具。临场可核对的触发条件随观察写入回放。"""
    thresholds = set()
    triggers = set()
    for key in selection.owned:
        if not key.startswith("p_item_"):
            continue
        row = selection.rows.get(key, {})
        if row.get("mode") != "stage":
            continue
        for event, body in _statements(row.get("effects", "")):
            if not event.startswith("at:"):
                continue
            phase = event[3:]
            for command, nested in body:
                terms = command[3:].split(" & ") if command.startswith("if:") else []
                effects = nested if terms else [(command, [])]
                if not any(cmd.startswith(("goodImpressionTurns+=", "goodImpressionTurns*=", "score+=goodImpressionTurns",
                                           "setGoodImpressionTurns")) for cmd, _ in effects):
                    continue
                if phase == "endOfTurn" and len(terms) == 1:
                    threshold = re.fullmatch(r"goodImpressionTurns>=(\d+)", terms[0])
                    if threshold:
                        thresholds.add(int(threshold[1]))
                elif phase in ("afterCardUsed", "cardUsed", "goodImpressionTurnsIncreased", "motivationIncreased", "buffCostConsumed",
                               "afterCardUsed[mental]", "afterCardUsed[active]", "cardUsed[mental]", "cardUsed[active]", "mentalCardUsed", "activeCardUsed"):
                    triggers.add((phase, tuple(terms)))
    return {"item_impression_thresholds": sorted(thresholds), "item_card_triggers": sorted(triggers)}


def _compare_conversion(hand, turns, multiplier, peak, reserve_percent):
    """比较占用本次行动的牌。估计新层数的后续转分，并为一次性牌预留高倍率回合的价差。"""
    paid = [roles for _, roles, _ in hand if roles and not roles.get("returns_use")]
    growth = max((r.get("impression", 0) for r in paid), default=0)
    if growth <= 0:
        return
    weight = multiplier + peak * (turns - 1) * (1 + reserve_percent / 100)

    def value(roles):
        rate = multiplier - (peak - multiplier if roles.get("single_use") else 0)
        return roles.get("direct_score", 0) * rate + roles.get("impression", 0) * weight

    best_growth = max(value(r) for r in paid if r.get("impression", 0) == growth)
    for roles in paid:
        if roles.get("impression_score", 0) > 0 and roles.get("direct_score", 0) > 0 and value(roles) > best_growth:
            roles["profitable_conversion"] = value(roles) - best_growth


def choose_battle_action(profile, observation):
    state = observation.get("battle_state") or {}
    fields = state.get("fields") or {}
    values = {b["key"]: b.get("value") for b in state.get("buffs", []) if b.get("key")}
    values.update({key: (fields.get(key) or {}).get("value") for key in ("genki", "stamina", "score")})
    values["maxStamina"] = observation.get("max_stamina")
    turns = (fields.get("turns_remaining") or {}).get("value")
    values["turnsRemaining"] = turns
    layout = state.get("layout")
    if not profile or not state.get("hud_present") or layout not in profile["layouts"] or type(turns) is not int or turns < 1:
        return {"action": "fallback", "reason": "配置范围或战斗回合未确认"}

    hand = []
    unknown = False
    drive_requirements = []
    visible = [c for c in observation.get("cards") or () if c.get("in_hand_region")]
    for card in visible:
        identity = card.get("identity") or {}
        roles = _card_roles(identity, values, profile, observation.get("item_impression_thresholds", []), observation.get("item_card_triggers", []))
        face = identity.get("effective_effect_dsl") or identity.get("reference_effect_dsl") or {}
        if roles is not None:
            drive_requirements.extend(int(n) for n in re.findall(r"motivation(?:>=|-=)(\d+)", face.get("conditions", "") + ";" + face.get("cost", "")))
        if not card.get("available"):
            continue
        if roles is None or not card.get("candidate_id"):
            unknown = True
        else:
            hand.append((card["candidate_id"], roles, card.get("label") == "suggestions"))

    drive = values.get("motivation")
    def productive(roles):
        return any(roles.get(key, 0) > 0 for key in ("impression", "impression_score", "engine", "extend", "direct_score", "persistent_score"))
    useful = sum(productive(r) for _, r, _ in hand)
    remaining = state.get("card_uses_remaining")
    # HUD 通常没有总次数。只有手牌可核实、没有可见追加次数标志时才允许按手牌判断。
    needs_extra = remaining == 1 if remaining is not None else not unknown and "additionalCardUses" not in values
    hand_tests = {"weak": bool(visible) and not unknown and not useful,
                  "needs_extra_use": needs_extra and not observation.get("extra_drink_used"),
                  "sustained_drive": bool(drive_requirements or profile["reserve_recovery"] or any(r.get("drive_score") for _, r, _ in hand)),
                  "needs_drive": drive is not None and any(drive < n for n in drive_requirements),
                  "unlock_drive": drive is not None and any(drive < n <= drive + 3 for n in drive_requirements)}
    for tag, role in (("conversion", "impression_score"), ("active_conversion", "active_conversion"), ("active", "active"), ("upgradable", "upgradable")):
        hand_tests[tag] = any(r.get(role, 0) > 0 for _, r, _ in hand)
    drinks = []
    for bottle in observation.get("drinks") or ():
        identity = bottle.get("identity") or {}
        role = profile["drink_effects"].get((identity.get("reference_effect_dsl") or {}).get("actions"))
        if layout in profile["drink_layouts"] and role and bottle.get("slot") in (1, 2, 3, 4) and identity.get("catalog_id"):
            drinks.append((bottle["slot"], {role: 1}, False))
    paid_uses = sum(productive(r) and not r.get("returns_use") for _, r, _ in hand)
    multiplier = (fields.get("attribute_multiplier_percent") or {}).get("value")
    peak = observation.get("peak_attribute_multiplier_percent") or multiplier
    if (layout == "exam" and multiplier and multiplier > 0 and profile.get("future_conversion_percent") is not None
            and needs_extra):
        _compare_conversion(hand, turns, multiplier, max(multiplier, peak), profile["future_conversion_percent"])
    limits = {"turns": turns, "genki": values["genki"], "impression": values.get("goodImpressionTurns"), "stamina": values["stamina"],
              "multiplier": multiplier, "peak_percent": multiplier * 100 // peak if multiplier is not None and peak else None, "useful_cards": paid_uses}
    eligible = []
    next_card = None
    for rule in profile["rules"]:
        if rule.get("once_per_battle") and rule["id"] in observation.get("used_drink_rules", ()):
            continue
        if any(limits[k[4:]] is None or (limits[k[4:]] < n if k.startswith("min_") else limits[k[4:]] > n)
               for k, n in rule.items() if k.startswith(("min_", "max_"))):
            continue
        if rule.get("hand") and not hand_tests[rule["hand"]]:
            continue
        if "remaining_card_uses" in rule and state.get("card_uses_remaining") != rule["remaining_card_uses"]:
            continue
        play = rule["action"] == "play_card"
        role = rule["role"]
        matches = [c for c in (hand if play else drinks) if c[1].get(role, 0) > 0]
        if matches:
            preserve_limited = play and layout == "exam" and turns > 1 and role in {"output", "impression_score", "direct_score"}
            target, roles, _ = max(matches, key=lambda c: (c[1][role], c[1].get("affinity", 0),
                                                           preserve_limited and not c[1].get("single_use"), c[2]))
            eligible.append((rule, target))
            if play:
                next_card = roles
                break
    for rule, target in eligible:
        if rule.get("next_card") and not (next_card or {}).get(rule["next_card"]):
            continue
        if rule["action"] == "use_drink" and rule["role"] == "impression_score" and (next_card or {}).get("extra_impression", 0) > 0:
            # 先打叠层且返还行动的牌。用瓶窗口仍在，操作后重新读数。
            continue
        return {"action": rule["action"], "candidate_id" if rule["action"] == "play_card" else "slot": target,
                "rule_id": rule["id"], "reason": rule["reason"]}
    return {"action": "fallback", "reason": "可用手牌身份未确认" if unknown else "当前候选和状态未命中规则"}
