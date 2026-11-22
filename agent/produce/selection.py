"""培育奖励与持有卡选择。输入是已识别的库 ID，输出档位和简短理由。"""

import re
import json
import unicodedata
from pathlib import Path
from functools import lru_cache
from collections import defaultdict

from .resources import REWARD_RULES_NAME, data_directory

RESOURCES = {
    "goodConditionTurns": "good", "perfectConditionTurns": "perfect", "concentration": "focus",
    "motivation": "drive", "genki": "guard", "fixedGenki": "guard", "goodImpressionTurns": "impression",
    "fullPowerCharge": "charge", "cardUsesRemaining": "extra", "turnsRemaining": "extend",
    "stamina": "heal", "fixedStamina": "heal",
}
LABELS = dict(good="好调", perfect="绝好调", focus="集中", drive="干劲", guard="元气", impression="好印象",
              charge="全力值", fire="强气", ice="温存", delayed_ice="下回合温存", switch_score="直接切换成长",
              focus_score="集中转分", guard_score="元气转分", impression_score="好印象转分", full_score="全力输出",
              charge_score="累计全力值转分", score="得分", draw="抽牌", extra="追加行动", extend="延长回合", heal="回复体力", hold="保留手牌")
NO_MATCH = (0, "")


def debug_choice(capture, context, image, operation, choice):
    """临时调试截图。稳定后可移除此函数及调用点，评分和点击均独立。"""
    if capture.save_screenshots:
        capture.record("selection_debug", image=image, context=context, save_frame=True, operation=operation, choice=choice)


@lru_cache(maxsize=1)
def rules():
    path = data_directory(Path(__file__).resolve().parents[2]) / REWARD_RULES_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def direct_actions(text):
    """移除条件、目标和未来时点的块，只保留当前层的语句。"""
    while "{" in text:
        stripped = re.sub(r"[^;{}]*\{[^{}]*\}", "", text)
        if stripped == text:
            return ""
        text = stripped
    return text


def face_roles(row, actions=None):
    if actions is None:
        actions = row.get("actions", "")
    direct = direct_actions(actions)
    roles = {}
    for name, value in re.findall(r"(?<![\w.])(\w+)\+=(\d+(?:\.\d+)?)(?=\s*(?:;|$))", direct):
        if name in RESOURCES or name == "score":
            roles[RESOURCES.get(name, name)] = float(value)
    for stance, role in (("strength", "fire"), ("preservation", "ice")):
        if re.search(rf"setStance\({stance}2?\)", direct):
            roles[role] = 1
    for name, role in (("genki", "guard_score"), ("goodImpressionTurns", "impression_score"), ("cumulativeFullPowerCharge", "charge_score")):
        scale = re.search(rf"score\+={name}\b(?:\*([\d.]+))?", direct)
        if scale:
            roles[role] = float(scale[1] or 1)
    focus = re.search(r"concentrationMultiplier=([\d.]+)", direct)
    if focus and "score+=" in direct:
        roles["focus_score"] = float(focus[1])
    full = re.search(r"if:isFullPower\s*\{\s*score\+=(\d+)", actions)
    if full or row.get("conditions") == "if:isFullPower" and "score" in roles:
        roles["full_score"] = int(full[1]) if full else roles["score"]
    if re.search(r"at:turn\s*\{\s*setStance\(preservation2?\)", actions):
        roles["delayed_ice"] = 1
    if re.search(r"at:stanceChanged\s*\{\s*if:isDirectEffect[^{}]*\{\s*target:this\s*\{\s*g\.score", row.get("effects", "")):
        roles["switch_score"] = 1
    for pattern, role in ((r"\bdrawCard\b", "draw"), (r"\bhold(?:Selected|ThisCard)\b", "hold")):
        if re.search(pattern, direct):
            roles[role] = 1
    return roles


def song_id(data, idol, song):
    def key(value):
        return "".join(unicodedata.normalize("NFKC", value).split())

    # 配置可以使用简中译名。先在同一条记录中还原偶像和歌曲，再按稀有度精确关联。
    ids = set()
    for rarity, singers in rules()["song_index"].items():
        for row in data.get(rarity, []):
            if key(idol) in (key(row["偶像名称"]), key(row["偶像中文"])) and key(song) in (key(row["歌曲名称"]), key(row["歌曲中文"])):
                ids.add(singers.get(key(row["偶像名称"]), {}).get(key(row["歌曲名称"])))
    return ids.pop() if len(ids) == 1 else None


class SelectionPolicy:
    def __init__(self, catalog, effect, idol_id, scenario):
        self.catalog = catalog
        self.effect = effect
        self.idol_id = idol_id
        self.scenario = scenario
        self.snapshot_matches = catalog.hashes == rules()["catalog_sha256"]
        self.rows = {key: row for key, (_, row) in catalog.ids.items()}
        self.roles = {key: face_roles(row) for key, row in self.rows.items()}
        self.intrinsics = {key for key, row in self.rows.items() if self.snapshot_matches and idol_id and
                           catalog.ids[key][0] == "skill_card" and row.get("pIdolId") == idol_id}
        # 开局没见过牌面时，只采用该歌曲所有可能固有版本共有的费用。
        costs = [dict(self.typed_cost(key)) for key in self.intrinsics]
        self.common_costs = {role: min(cost.get(role, 0) for cost in costs) for role in costs[0]} if costs else {}
        self.owned = set()
        self.supply = {}
        self.full_power_seen = False
        self.item_triggers = {}
        self.attributes = {}
        self.health = None
        self.upgrades = {}
        self.skill_versions = defaultdict(list)
        families = defaultdict(dict)
        for key, row in self.rows.items():
            if catalog.ids[key][0] == "skill_card":
                self.skill_versions[row["name"].rstrip("+")].append(key)
            for game in row.get("game_keys", []):
                if game["table"] == "ProduceCard":
                    families[game["id"]].setdefault(game.get("upgradeCount", 0), set()).add(key)
        for versions in families.values():
            if len(versions.get(1, ())) == 1:
                for key in versions.get(0, ()):
                    self.upgrades[key] = next(iter(versions[1]))

    def observe(self, ids):
        for key in set(ids).intersection(self.rows) - self.owned:
            self.owned.add(key)
            if self.catalog.ids[key][0] == "skill_card" and not self.rows[key].get("conditions") and not self.typed_cost(key):
                for role, amount in self.roles[key].items():
                    self.supply[role] = max(amount, self.supply.get(role, 0))

    def acquired(self, key):
        """领取核验并离开原奖励页后才登记。饮料的实际保留以满栏界面为准。"""
        if key not in self.rows or self.catalog.ids[key][0] == "drink":
            return
        if self.snapshot_matches and self.catalog.ids[key][0] == "skill_card":
            for item in self.item_triggers:
                event = rules()["p_item_events"][item]
                if self.acquisition_event(event, key):
                    self.item_triggers[item] += 1
        self.observe([key])
        if self.snapshot_matches and rules()["p_item_events"].get(key, {}).get("event") == "skill_acquired":
            self.item_triggers.setdefault(key, 0)

    def acquisition_event(self, event, key):
        if event["event"] != "skill_acquired" or event.get("fire_interval", 0) or "game_effect_tags" in event:
            return False
        for field, value in event.get("requires", {}).items():
            if field == "scenario" and value == self.scenario:
                continue
            if not field.endswith("_min") or self.attributes.get(field[:-4], -1) < value:
                return False
        tags = event.get("game_effect_tags_any", [])
        return not tags or bool(self.roles[key].keys() & set(tags))

    def forget_card(self, removed, base_name=None):
        if removed or base_name:
            variants = {removed} if removed else set(self.skill_versions[base_name])
            retained = self.owned - variants
        else:
            retained = {k for k in self.owned if self.catalog.ids[k][0] == "p_item"}
        self.owned.clear()
        self.supply.clear()
        self.observe(retained)

    def typed_cost(self, key):
        return [(RESOURCES[name], int(n)) for name, n in re.findall(r"(\w+)-=(\d+)", self.rows[key].get("cost", ""))
                if name in RESOURCES and name != "stamina"]

    def usable(self, key):
        row = self.rows[key]
        if any(self.supply.get(role, 0) < cost for role, cost in self.typed_cost(key)):
            return False
        condition = row.get("conditions", "")
        if not condition:
            return True
        if condition in ("if:isFullPower", "if:isStrength", "if:isPreservation", "if:stance!=none"):
            return {"if:isFullPower": self.full_power_seen, "if:isStrength": "fire" in self.supply,
                    "if:isPreservation": "ice" in self.supply, "if:stance!=none": bool(self.supply.keys() & {"fire", "ice"})}[condition]
        threshold = re.fullmatch(r"if:(\w+)>=(\d+)", condition)
        return bool(threshold and self.supply.get(RESOURCES.get(threshold[1]), 0) >= int(threshold[2]))

    def removal_score(self, key):
        row = self.rows[key]
        if row.get("type") == "trouble" and not row.get("actions") and not row.get("effects"):
            return 82, "移除障碍牌"
        # 同费用或更便宜的纯得分牌已实见，才考虑舍弃基础得分牌。
        if row.get("sourceType") == "default" and set(self.roles[key]) == {"score"}:
            cost = re.fullmatch(r"cost-=(\d+)", row["cost"])
            for held in self.owned:
                other = self.rows[held]
                price = re.fullmatch(r"cost-=(\d+)", other.get("cost", ""))
                if (cost and price and int(price[1]) <= int(cost[1]) and not other["conditions"]
                        and self.roles[held].get("score", 0) > self.roles[key]["score"]):
                    return 48, "已有更省费或同费的高分牌"
        return NO_MATCH

    def upgrade_score(self, key):
        row = self.rows[key]
        upgraded = self.upgrades.get(key)
        grade, reason = self.evaluate(upgraded, "upgrade_result")
        if not grade:
            return NO_MATCH
        values = self.roles[upgraded]
        if self.usable(key) and not any(values.get(k, 0) > self.roles[key].get(k, 0) for k in values) and row["cost"] == self.rows[upgraded]["cost"]:
            return NO_MATCH
        return grade, "强化后" + reason

    def evaluate(self, key, operation="receive"):
        if key not in self.rows:
            return NO_MATCH
        row = self.rows[key]
        if operation in {"delete", "exchange"}:
            return self.removal_score(key)
        if operation == "copy" and row.get("unique"):
            return NO_MATCH
        if operation == "strengthen":
            return self.upgrade_score(key)
        if not self.usable(key) or operation != "upgrade_result" and row.get("unique") and key in self.owned:
            return NO_MATCH
        active_roles = set(self.supply)
        if self.full_power_seen:
            active_roles.add("can_enter_full")
        profile = self.effect
        if profile.startswith("非凡") and any("switch_score" in self.roles[k] for k in self.owned) and active_roles & {"fire", "ice"}:
            profile = "冰火"
        offers = self.roles[key]
        ranked = [NO_MATCH]
        kind = self.catalog.ids[key][0]
        health_cost = re.fullmatch(r"stamina-=(\d+)", row.get("cost", ""))
        if health_cost and self.health and self.health["current"] <= int(health_cost[1]):
            return NO_MATCH
        if kind == "drink":
            hp = re.search(r"fixedStamina-=(\d+)", row["actions"])
            if hp and (not self.health or self.health["current"] <= int(hp[1])):
                return NO_MATCH
            case = rules()["drink_cases"].get(key, {}) if self.snapshot_matches else {}
            needs = case.get("requires")
            if needs in {"future_start_turns", "future_end_turns"}:
                offers = dict.fromkeys(case["roles"], 1)
            if "score_buff" in case.get("roles", []) and "score" in active_roles:
                ranked.append((53, "已有得分牌可用饮料增幅"))
            if "extra" in offers and self.owned and active_roles & {"score", "focus_score", "guard_score", "impression_score", "full_score"}:
                ranked.append((55, "追加行动可再打一张输出牌"))
        if kind == "p_item":
            # 只展开已确认时点和可支付的单层条件，其他复杂触发留给专项规则。
            trigger = re.fullmatch(
                r"at:(startOfStage|startOfTurn|endOfTurn|afterStartOfTurn) \{ (?:if:(\w+)>=(\d+) \{ ([^{}]+) \}|([^{}]+?))(?:; limit:\d+)? \}",
                row["effects"],
            )
            if trigger and (not trigger[2] or self.supply.get(RESOURCES.get(trigger[2]), 0) >= int(trigger[3])):
                offers = face_roles(row, trigger[4] or trigger[5])
        if kind == "drink" and self.health and self.health["current"] < self.health["max"] and offers.get("heal"):
            ranked.append((77 if self.health["ratio"] < .4 else 36, "回复已损失的体力"))
        if kind == "skill_card" and operation in {"receive", "copy"}:
            for item, count in self.item_triggers.items():
                event = rules()["p_item_events"][item]
                if (not event["fire_limit"] or count < event["fire_limit"]) and self.acquisition_event(event, key):
                    ranked.append((72, "领取可触发" + self.catalog.display_name(item)))
        if kind == "p_item" and self.snapshot_matches:
            threshold = next((r["when"] for r in rules()["p_item_thresholds"] if r["id"] == key), None)
            if threshold and self.supply.get(threshold["resource"], 0) >= threshold["min"]:
                ranked.append((72, "已有" + LABELS[threshold["resource"]] + "来源，可触发道具"))
            event = rules()["p_item_events"].get(key, {})
            tags = event.get("game_effect_tags_any", [])
            if (event.get("event") == "skill_acquired" and tags and event["requires"] == {"scenario": self.scenario}
                    and any(g["role"] in tags for g in rules()["profiles"].get(profile, []))):
                ranked.append((56, "后续领取本流派卡可增加投票"))
        for goal in rules()["profiles"].get(profile, []):
            role = goal["role"]
            if role in offers and set(goal["requires_owned"]) <= active_roles:
                grade = goal["grade"] if role not in active_roles or operation == "upgrade_result" else 30
                reason = f"补{LABELS[role]}"
                ranked.append((grade, f"{profile}·{reason}" if profile != self.effect else reason))
        demands = list(self.common_costs.items())
        for held in self.owned & self.intrinsics:
            demands.extend(self.typed_cost(held))
        for role, cost in demands:
            if role in offers and self.supply.get(role, 0) < cost:
                ranked.append((89, "补固有卡的" + LABELS[role] + "费用"))
        if self.snapshot_matches and self.idol_id:
            ranked.extend(self.song_matches(key, offers, operation))
        return max(ranked, key=lambda match: match[0])

    def song_matches(self, key, offers, operation):
        skill = self.catalog.ids[key][0] == "skill_card"
        roles = set(offers)
        for source, tag in (("charge", "charge_card"), ("ice", "preservation_card")):
            if skill and source in roles:
                roles.add(tag)
        facts = {
            "ice_window": "ice" in self.supply, "missing_switch_source": "fire" not in self.supply,
            "can_enter_full": self.full_power_seen, "play_in_full": self.full_power_seen and self.usable(key),
            "cumulative_charge_positive": self.full_power_seen,
            "two_plays_in_full_reachable": self.full_power_seen and "extra" in roles and "score" in self.supply,
            "impression_positive": "impression" in self.supply, "direct_impression_increase": "impression" in self.roles[key],
            "drive_below_6": self.supply.get("drive", 0) < 6, "direct_drive_available": "drive" in self.supply,
            "impression_below_6": self.supply.get("impression", 0) < 6,
            "target_can_score": "score" in roles, "can_play_charge_card": "charge_card" in roles,
            "held_target_usable": "score" in self.supply, "has_future_triggers": skill,
            "cumulative_charge_below_10": not self.full_power_seen and 0 < self.supply.get("charge", 0) < 10,
            "threshold_reachable": self.supply.get("charge", 0) + offers.get("charge", 0) >= 10,
        }
        for rule in rules()["song_rules"]:
            held = self.owned.intersection(rule["held_any"])
            if self.idol_id != rule["pIdolId"] or not held:
                continue
            role = rule["role"]
            required = rule["requires_all"]
            if role == "named_target":
                if key not in rule["candidate_ids"] or operation not in {"receive", "upgrade_result"}:
                    continue
                facts["missing_target"] = key not in self.owned
                facts["can_retrieve_from_deck_or_discard"] = any(f"id({self.rows[key]['id']})" in self.rows[k].get("actions", "") for k in held)
                reason = "满足固有卡或道具的指定目标"
            else:
                if role not in roles:
                    continue
                source = {"charge_card": "charge", "preservation_card": "ice"}.get(role, role)
                facts["missing_target"] = source not in self.supply
                reason = "补固有联动所需的" + LABELS[source]
            if all(facts[f] for f in required):
                yield rule["grade"], reason

    def evaluate_candidate(self, candidate, operation):
        """牌名确认时按可适用版本的较低档位评估，保留实际版本未知。"""
        key = candidate.get("catalog_id")
        if key:
            return self.evaluate(key, operation)
        versions = self.skill_versions.get(candidate.get("base_name"), ())
        if operation == "strengthen":
            # 普通强化列表供玩家选可强化的牌，只比较基础版的强化收益。
            versions = [key for key in versions if not self.rows[key].get("upgraded")]
        grade, reason = min((self.evaluate(key, operation) for key in versions), default=NO_MATCH, key=lambda score: score[0])
        return (grade, f"{reason}（按牌名）") if grade else NO_MATCH

    def choose(self, candidates, operation):
        """同档保留候选顺序；只有所有候选均未命中才返回默认分支。"""
        best = None
        best_rank = (0, 0, False)
        for candidate in candidates:
            grade, reason = self.evaluate_candidate(candidate, operation)
            rank = (grade, -candidate.get("price", 0), bool(candidate.get("catalog_id")))
            if grade and rank > best_rank:
                best_rank = rank
                best = {**candidate, "grade": grade, "reason": reason}
        return best

    def keep_drinks(self, bottles):
        """满栏先补不同的已知用途；同档保留游戏勾选的实例。"""
        scores = {key: self.evaluate(key, "keep_drink") for key in {b[1] for b in bottles}}
        if len(bottles) <= 3 or not any(grade for grade, _ in scores.values()):
            return [], ""
        remaining = list(bottles)
        keep = []
        covered = set()
        for _ in range(3):
            def priority(bottle):
                grade, reason = scores[bottle[1]]
                return (grade if reason not in covered else 0, bottle[2], grade)
            best = max(remaining, key=priority)
            keep.append(best)
            covered.add(scores[best[1]][1])
            remaining.remove(best)
        return keep, scores[keep[0][1]][1]
