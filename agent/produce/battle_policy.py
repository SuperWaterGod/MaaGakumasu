"""按流派评价当前画面，只返回一个可执行目标。"""

import re
import json
import math
import operator
from functools import lru_cache
from collections import Counter

from .selection import direct_actions

CARD_ROLES = {"extend", "item_sequence", "recovery", "extra_score", "extra_impression", "cycle", "engine", "multiply", "threshold", "sustain",
              "output", "impression_score", "profitable_conversion", "impression", "direct_score", "economy", "drive", "guard", "support",
              "extra_drive", "persistent_score", "score_boost", "exchange", "free_setup", "register_effect", "active_guard_setup", "peak_conversion"}
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
        roles = {"use_drink": set(profile["drink_effects"].values()) | {"item_boost"}, "play_card": CARD_ROLES}
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
    """流派决定规则，已核实的歌曲身份只用于检索初始道具。"""
    levels = {"初": ("REGULAR", "PRO", "MASTER"), "NIA": ("PRO", "MASTER")}
    if difficulty not in levels.get(scenario, ()):
        return None
    return next(({**p, "idol_id": song_id, "verified_upgrades": config.get("verified_upgrades", {})}
                 for p in config["profiles"] if p["effect"] == effect), None)


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


def _card_roles(identity, state_values, profile, item_thresholds, item_triggers=(), items=None):
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
    card_conversion = facts["conversion"]
    spent = []
    projected = False
    # 先结算卡牌的回复，再结算出牌后发动的道具。
    if values.get("stamina") is not None and facts["fixedStamina"]:
        values["stamina"] += math.ceil(facts["fixedStamina"])
        if values.get("maxStamina") is not None:
            values["stamina"] = min(values["stamina"], values["maxStamina"])
    if values.get("genki") is not None:
        values["genki"] = max(0, values["genki"] + facts["fixedGenki"])
    for key, variants in (items or {}).items():
        result, possible = _project_item(variants, identity, face, values)
        if possible:
            spent.append(key)
        if result is not None:
            values, bonus = result
            facts.update(bonus)
            projected |= bool(bonus["score"] or bonus["goodImpressionTurns"] or bonus["cardUsesRemaining"])
    if spent and values.get("stamina") is not None and values["stamina"] <= 0:
        return {}
    growth = facts["goodImpressionTurns"] - (int(growth_cost[1]) if growth_cost else 0)
    has_extra = facts["cardUsesRemaining"] > 0
    score = facts["score"]
    heal = facts["fixedStamina"] + facts["stamina"]
    guard = facts["genki"] + facts["fixedGenki"]
    impression = state_values.get("goodImpressionTurns")
    roles = {"impression": growth, "direct_score": score, "impression_score": facts["conversion"], "extra_score": facts["conversion"] if has_extra else 0,
             "single_use": face.get("limit") == 1, "peak_conversion": facts["conversion"],
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
             "exchange": int("exchangeHand" in actions), "changes_hand": "exchangeHand" in actions or "upgradeHand" in actions,
             "after": values, "item_uses": spent, "item_gain": projected}
    if impression is not None and any(impression < threshold <= impression + growth for threshold in item_thresholds):
        roles["threshold"] = growth
    if roles["output"] > 0 and (identity.get("upgraded") is False or face.get("upgraded") is False):
        roles["upgradable"] = 1
    if card_conversion and identity.get("type", face.get("type")) == "active" and identity.get("rarity", face.get("rarity")) in ("N", "R", "SR", "SSR"):
        roles["active_conversion"] = 1
    if item_triggers:
        roles["affinity"] = _item_affinity(item_triggers, identity, face, values, facts)
    return roles


@lru_cache(maxsize=512)
def _item_effect(text):
    """只解析顺序明确、可逐项结算的出牌后效果，保留未知原文供回放。"""
    steps = _statements(text)
    if len(steps) != 1 or steps[0][0] not in ("at:afterCardUsed", "at:afterCardUsed[active]", "at:afterCardUsed[mental]"):
        return None
    phase, body = steps[0]
    limit = next((int(c[6:]) for c, _ in body if re.fullmatch(r"limit:\d+", c)), None)
    actions = [(c, b) for c, b in body if not re.fullmatch(r"limit:\d+", c)]
    condition = ""
    if len(actions) == 1 and actions[0][0].startswith("if:"):
        condition, actions = actions[0][0][3:], actions[0][1]
    scalar = r"(?:goodImpressionTurns|genki|fixedStamina|motivation|cardUsesRemaining|score)"
    operand = r"(?:goodImpressionTurns|genki|motivation|[0-9]+(?:\.[0-9]+)?)"
    assignment = rf"{scalar}(?:\+=|-=|\*=|=){operand}(?:\*{operand})*"
    if (not actions or any(b or not re.fullmatch(assignment, c) for c, b in actions)
            or any(not re.fullmatch(r"cardHasEffect\(\w+\)|(?:motivation|genki|goodImpressionTurns)>=\d+", term)
                   for term in condition.split(" & ") if term)):
        return None
    return phase, condition, actions, limit


def _project_item(variants, identity, face, values):
    """未知强化时取各可能版本的保守收益；次数只在可能触发时预留。"""
    outcomes = []
    for text in variants:
        phase, condition, steps, _ = _item_effect(text)
        kind = identity.get("type") or face.get("type")
        if "[" in phase and phase != f"at:afterCardUsed[{kind}]":
            return None, kind is None
        for term in condition.split(" & "):
            if term.startswith("cardHasEffect("):
                effect = r"\b" + re.escape(term.removeprefix("cardHasEffect(")[:-1]) + r"\+="
                actions = face.get("actions", "")
                if not re.search(effect, direct_actions(actions)):
                    return None, bool(re.search(effect, actions))
            elif term:
                field = term.split(">=")[0]
                if values.get(field) is None:
                    return None, True
                if not _condition(term, values):
                    return None, False
        after = values.copy()
        bonus = Counter()
        for cmd, _ in steps:
            expr = cmd.split("=", 1)[1]
            if _number(expr, after) is None:
                # 未读到元气时仍可核算原有好印象的保底转分，不虚构额外层数。
                if re.fullmatch(r"(?:score|goodImpressionTurns)\+=genki(?:\*[\d.]+)?", cmd):
                    continue
                return None, True
            _evaluate([(cmd, [])], after, bonus)
            if cmd.startswith("goodImpressionTurns") and after.get("goodImpressionTurns") is not None:
                rounded = math.ceil(round(after["goodImpressionTurns"], 2))
                bonus["goodImpressionTurns"] += rounded - after["goodImpressionTurns"]
                after["goodImpressionTurns"] = rounded
        if after.get("stamina") is not None:
            after["stamina"] += bonus["fixedStamina"]
            if after.get("maxStamina") is not None:
                after["stamina"] = min(after["stamina"], after["maxStamina"])
        outcomes.append((after, bonus))
    return min(outcomes, key=lambda pair: (pair[1]["score"], pair[1]["goodImpressionTurns"], pair[1]["cardUsesRemaining"])), True


def intrinsic_battle_items(rows, idol_id):
    if idol_id is None:
        return ()
    matches = {key: row for key, row in rows.items() if key.startswith("p_item_") and row.get("sourceType") == "pIdol"
               and row.get("pIdolId") == idol_id}
    return tuple(matches) if len({row["name"].rstrip("+") for row in matches.values()}) == 1 else ()


def battle_item_conditions(selection, intrinsic_items=()):
    """汇总已持有和已绑定偶像的初始道具；未核实强化时保留全部可能版本。"""
    thresholds = set()
    triggers = set()
    groups = {}
    unsupported = {}
    for key in sorted(set(intrinsic_items) | set(selection.owned)):
        row = selection.rows.get(key, {})
        if not key.startswith("p_item_") or row.get("mode") != "stage":
            continue
        family = row.get("name", key).rstrip("+")
        groups.setdefault(family, []).append((key, row.get("effects", "")))
    items = {}
    for family, candidates in groups.items():
        observed = [text for key, text in candidates if key in selection.owned]
        variants = sorted(set(observed or [text for _, text in candidates]))
        compiled = [_item_effect(v) for v in variants]
        if all(compiled) and len({(c[0], c[1], c[3]) for c in compiled}) == 1:
            items[family] = variants
        elif any(variants):
            unsupported[family] = variants
    for key in selection.owned:
        if not key.startswith("p_item_"):
            continue
        row = selection.rows.get(key, {})
        if row.get("mode") != "stage":
            continue
        if row.get("name", key).rstrip("+") in items:
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
    return {"item_impression_thresholds": sorted(thresholds), "item_card_triggers": sorted(triggers),
            "battle_items": items, "unmodeled_item_effects": unsupported}


def _compare_conversion(hand, turns, multiplier, peak, reserve_percent):
    """比较占用最后一次行动的牌。手牌将弃置，当前输出按本回合倍率，新增层数保留未来转分价值。"""
    paid = [roles for _, roles, _ in hand if roles and not roles.get("returns_use")]
    growth = max((r.get("impression", 0) for r in paid), default=0)
    if growth <= 0:
        return
    weight = multiplier + peak * (turns - 1) * (1 + reserve_percent / 100)

    def value(roles):
        return roles.get("direct_score", 0) * multiplier + roles.get("impression", 0) * weight

    best_growth = max(value(r) for r in paid if r.get("impression", 0) == growth)
    reusable_output = any(r.get("impression_score") and not r.get("single_use") and value(r) > best_growth for r in paid)
    for roles in paid:
        if roles.get("single_use") and value(roles) < best_growth:
            roles["peak_conversion"] = 0
        if roles.get("impression_score", 0) > 0 and roles.get("direct_score", 0) > 0 and value(roles) > best_growth:
            advantage = value(roles) - best_growth
            if reusable_output and roles.get("single_use"):
                advantage -= roles["direct_score"] * (peak - multiplier)
            roles["profitable_conversion"] = advantage


def available_battle_items(observation):
    """次数表示本场已可能消耗的上限，未知出牌也占用一次限次机会。"""
    spent = observation.get("spent_item_uses", {})
    unknown = observation.get("untracked_card_uses", 0)
    items = {}
    for key, variants in observation.get("battle_items", {}).items():
        effect = _item_effect(variants[0]) if variants else None
        if effect and (effect[3] is None or (unknown is not None and spent.get(key, 0) + unknown < effect[3])):
            items[key] = variants
    return items


def _compare_item_sequences(hand, identities, profile, items, observation, turns):
    """仅展开当前手里的一张后续牌。未知抽牌与换手在重新截图后处理。"""
    if not any(r.get("item_gain") for _, r, _ in hand):
        return
    spent = observation.get("spent_item_uses", {})
    def value(r):
        return r.get("direct_score", 0) + r.get("impression", 0) * turns + r.get("persistent_score", 0)

    options = []
    for key, roles, _ in hand:
        after = roles.get("after")
        if not after or (after.get("stamina") is not None and after["stamina"] <= 0):
            continue
        points = value(roles)
        linked = roles["item_gain"]
        uses = observation["battle_state"].get("card_uses_remaining")
        if (roles["returns_use"] or (uses is not None and uses > 1)) and not roles["changes_hand"]:
            remaining = available_battle_items({**observation, "battle_items": items, "spent_item_uses": Counter(spent) + Counter(roles["item_uses"])})
            continuations = []
            for other, _, _ in hand:
                if key != other:
                    tail = _card_roles(identities[other], after, profile, (), items=remaining)
                    if tail and (tail["after"].get("stamina") is None or tail["after"]["stamina"] > 0):
                        continuations.append((value(tail), tail["item_gain"]))
            gain, uses_item = max(continuations, default=(0, False))
            points += gain
            linked |= uses_item
        options.append((points, linked, roles))
    best = max((p for p, _, _ in options), default=0)
    for points, linked, roles in options:
        if linked and points == best and points > 0:
            roles["item_sequence"] = points


def _boosting_drink(drinks, profile, values, items, identity, card):
    """只提前用能提高下一张牌联动得分的瓶。饮料生效后再读手牌。"""
    best_gain, slot = 0, None
    for bottle in drinks:
        face = bottle.get("identity") or {}
        actions = (face.get("reference_effect_dsl") or {}).get("actions", "")
        if (not face.get("catalog_id") or bottle.get("slot") not in (1, 2, 3, 4) or actions not in profile["drink_effects"]
                or not re.fullmatch(r"(?:genki|motivation|goodImpressionTurns)\+=\d+", actions)):
            continue
        after = values.copy()
        _evaluate(_statements(actions), after, Counter())
        boosted = _card_roles(identity, after, profile, (), items=items)
        gain = boosted["direct_score"] - card["direct_score"] if boosted and boosted.get("item_gain") else 0
        if gain > best_gain:
            best_gain, slot = gain, bottle["slot"]
    return slot


def _battle_values(observation):
    state = observation.get("battle_state") or {}
    fields = state.get("fields") or {}
    values = {b["key"]: b.get("value") for b in state.get("buffs", []) if b.get("key")}
    values.update({key: (fields.get(key) or {}).get("value") for key in ("genki", "stamina", "score")})
    values["maxStamina"] = observation.get("max_stamina")
    turns = (fields.get("turns_remaining") or {}).get("value")
    values["turnsRemaining"] = turns
    return values


def _policy_identity(profile, observation, card):
    """只在相同库指纹、精确名称和核验过的强化层数下还原费用，原始识别仍保留。"""
    identity = (card or {}).get("identity") or {}
    table = profile.get("verified_upgrades", {})
    if (identity.get("unknown_reason") != "extra_upgrade_markers" or identity.get("match_method") != "exact"
            or identity.get("similarity_margin", 0) <= 0 or identity.get("prefix_evidence") or identity.get("overlap_correction")
            or not observation.get("catalog_sha256") or table.get("catalog_sha256") != observation["catalog_sha256"]):
        return identity
    cost = table.get("costs", {}).get(f"{identity.get('source_id')}:{identity.get('observed_plus_count')}")
    face = identity.get("reference_effect_dsl")
    if type(cost) is not int or cost < 0 or not face:
        return identity
    return {**identity, "status": "matched", "card_id": identity["database_card_id"],
            "effective_effect_dsl": {**face, "cost": f"cost-={cost}"}, "verified_upgrade_cost": cost}


def possible_item_uses(profile, observation, card):
    """推荐或兜底出牌同样记账。无法识别时，所有可能触发的限次道具均保留一次不确定性。"""
    items = available_battle_items(observation)
    values = _battle_values(observation)
    roles = _card_roles(_policy_identity(profile, observation, card), values, profile, (), items=items) if values["turnsRemaining"] else None
    return roles["item_uses"] if roles else None


def choose_battle_action(profile, observation):
    state = observation.get("battle_state") or {}
    fields = state.get("fields") or {}
    values = _battle_values(observation)
    turns = values["turnsRemaining"]
    layout = state.get("layout")
    if not profile or not state.get("hud_present") or layout not in profile["layouts"] or type(turns) is not int or turns < 1:
        return {"action": "fallback", "reason": "配置范围或战斗回合未确认"}

    hand = []
    identities = {}
    items = available_battle_items(observation)
    unknown = []
    drive_requirements = []
    visible = [c for c in observation.get("cards") or () if c.get("in_hand_region")]
    for card in visible:
        identity = _policy_identity(profile, observation, card)
        roles = _card_roles(identity, values, profile, observation.get("item_impression_thresholds", []),
                            observation.get("item_card_triggers", []), items)
        face = identity.get("effective_effect_dsl") or identity.get("reference_effect_dsl") or {}
        if roles is not None:
            drive_requirements.extend(int(n) for n in re.findall(r"motivation(?:>=|-=)(\d+)", face.get("conditions", "") + ";" + face.get("cost", "")))
        if not card.get("available"):
            continue
        if roles is None or not card.get("candidate_id"):
            position = 1 + sum((other.get("box") or [0])[0] < (card.get("box") or [0])[0] for other in visible)
            unknown.append(identity.get("observed_name") or f"左起第{position}张（未读到牌名）")
        else:
            identities[card["candidate_id"]] = identity
            hand.append((card["candidate_id"], roles, card.get("label") == "suggestions"))

    _compare_item_sequences(hand, identities, profile, items, observation, turns)

    drive = values.get("motivation")
    def productive(roles):
        return any(roles.get(key, 0) > 0 for key in ("impression", "impression_score", "engine", "extend", "direct_score", "persistent_score"))
    useful = sum(productive(r) for _, r, _ in hand)
    remaining = state.get("card_uses_remaining")
    # HUD 通常没有总次数。只有手牌可核实、没有可见追加次数标志时才允许按手牌判断。
    needs_extra = remaining == 1 if remaining is not None else not unknown and "additionalCardUses" not in values
    hand_tests = {"weak": bool(visible) and not unknown and not useful,
                  "needs_extra_use": needs_extra and not observation.get("extra_drink_used"),
                  "sustained_drive": bool(drive_requirements or any(r.get("drive_score") for _, r, _ in hand)
                                          or any("+=genki*" in v for variants in items.values() for v in variants)),
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
    next_candidate = None
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
        if role == "item_boost":
            if items and drinks:
                eligible.append((rule, None))
            continue
        matches = [c for c in (hand if play else drinks) if c[1].get(role, 0) > 0]
        if matches:
            preserve_limited = play and layout == "exam" and turns > 1 and role in {"output", "impression_score", "direct_score"}
            target, roles, _ = max(matches, key=lambda c: (c[1][role], c[1].get("affinity", 0),
                                                           preserve_limited and not c[1].get("single_use"), c[2]))
            eligible.append((rule, target))
            if play:
                next_card = roles
                next_candidate = target
                break
    for rule, target in eligible:
        if rule["role"] == "item_boost":
            if not next_card or not next_card.get("item_gain"):
                continue
            target = _boosting_drink(observation["drinks"], profile, values, items, identities[next_candidate], next_card)
            if target is None:
                continue
        if rule.get("next_card") and not (next_card or {}).get(rule["next_card"]):
            continue
        if rule["action"] == "use_drink" and rule["role"] == "impression_score" and (next_card or {}).get("extra_impression", 0) > 0:
            # 先打叠层且返还行动的牌。用瓶窗口仍在，操作后重新读数。
            continue
        tracking = {"possible_item_uses": next_card["item_uses"]} if rule["action"] == "play_card" and items and next_card else {}
        reason = rule["reason"]
        if rule["action"] == "play_card" and "verified_upgrade_cost" in identities[target]:
            face = identities[target]
            reason += f"（{face['observed_name']}，已核对普通费用 {face['verified_upgrade_cost']}）"
        return {**tracking, "action": rule["action"], "candidate_id" if rule["action"] == "play_card" else "slot": target,
                "rule_id": rule["id"], "reason": reason}
    reason = "本帧无匹配规则"
    if unknown:
        reason += "，以下可用牌的身份或效果未核实：" + "、".join(unknown)
    return {"action": "fallback", "reason": reason, "retry_hand_read": bool(unknown)}
