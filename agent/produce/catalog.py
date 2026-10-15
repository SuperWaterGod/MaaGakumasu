"""技能卡、饮料和 P 道具的身份查库与结构化参考数据。"""

import copy
import json
import hashlib
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from collections import defaultdict


def normalize_name(text):
    text = unicodedata.normalize("NFKC", text).replace("＋", "+")
    return "".join(c for c in text if c.isalnum() or c == "+").casefold()


def split_variant(text):
    normalized = normalize_name(text)
    base = normalized.rstrip("+")
    return base, len(normalized) - len(base)


def name_match_allowed(observed, candidate, confidence, similarity, margin):
    """完整名称共用容错规则；短名称、歧义名称和强化标记仍严格核验。"""
    base, plus = split_variant(observed)
    target, target_plus = split_variant(candidate)
    single_substitution = len(base) == len(target) and len(base) >= 5 and sum(a != b for a, b in zip(base, target)) == 1
    return (confidence >= .65 and margin >= .08 and bool(plus) == bool(target_plus)
            and base.count("+") == target.count("+") and (similarity >= .90 or similarity >= .80 and single_substitution))


class SkillCardCatalog:
    def __init__(self, path: Path, aliases_path=None):
        raw = path.read_bytes()
        database = json.loads(raw)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.source = database["source"]
        self.cards = database["cards"]
        self.entries = []
        self.exact_names = set()
        aliases = {}
        if aliases_path and aliases_path.exists():
            aliases = json.loads(aliases_path.read_text(encoding="utf-8-sig"))
        for card in self.cards:
            names = [card["name"], *card.get("aliases", []), *aliases.get(str(card["id"]), [])]
            for name in names:
                base, plus = split_variant(name)
                self.entries.append((base, plus, card))
                self.exact_names.add(normalize_name(name))

    def _overlap_name(self, normalized):
        """完整库名称已经出现时，允许清理少量混入的相邻卡名尾字。"""
        prefixes = set()
        for base, variant_plus, _ in self.entries:
            if len(base) < 4 or not normalized.startswith(base):
                continue
            remainder = normalized[len(base) :]
            markers = len(remainder) - len(remainder.lstrip("+"))
            suffix = remainder[markers:]
            recognized = base + "+" * markers
            if bool(markers) == bool(variant_plus) and 0 < len(suffix) <= 2 and len(recognized) / len(normalized) >= 0.75:
                prefixes.add(recognized)
        return max(prefixes, key=len) if prefixes else None

    def _truncated_name(self, normalized):
        """按可见前缀覆盖率排列候选；基础版和强化版按同名归组。"""
        base, plus = split_variant(normalized)
        if len(base) < 3 or "+" in base:
            return None
        names = {candidate for candidate, _, _ in self.entries if candidate.startswith(base) and candidate != base}
        ranked = sorted(((len(base) / len(name), name) for name in names), reverse=True)
        if not ranked:
            return None
        coverage, name = ranked[0]
        margin = coverage - ranked[1][0] if len(ranked) > 1 else 1.0
        # 不同卡名共享同一可见前缀时，名称更短只提高长度覆盖率，
        # 不能因此确认其中某张卡。
        if coverage < 0.60:
            return None
        return {
            "name": name + "+" * plus,
            "visible_prefix": base,
            "coverage": coverage,
            "candidate_margin": margin,
            "ambiguous": len(ranked) > 1,
        }

    def match(self, text, ocr_confidence=1.0, *, allow_overlap=False):
        normalized = normalize_name(text)
        recognized = normalized
        prefix = None
        if allow_overlap and normalized not in self.exact_names:
            corrected = self._overlap_name(normalized)
            if corrected:
                recognized = corrected
            else:
                prefix = self._truncated_name(normalized)
                if prefix and not prefix["ambiguous"]:
                    recognized = prefix["name"]
        base, plus = split_variant(recognized)
        result = {
            "observed_name": text,
            "normalized_name": normalized,
            "recognized_name": recognized if recognized != normalized else text,
            "observed_plus_count": plus,
            "ocr_confidence": ocr_confidence,
            "status": "unknown",
            "card_id": None,
            "database_card_id": None,
            "effective_effect_dsl": None,
            "customization": None,
            "candidates": [],
        }
        if prefix:
            result["prefix_evidence"] = prefix
        elif recognized != normalized:
            result["overlap_correction"] = {"removed_suffix": normalized[len(recognized) :], "method": "complete_catalog_prefix"}
        if len(base) < 2:
            result["unknown_reason"] = "name_missing_or_too_short"
            return result
        scores = {}
        for candidate_base, candidate_plus, card in self.entries:
            # 可见加号选择强化条目；双加号仍标记为待确认的运行时变体。
            if bool(plus) != bool(candidate_plus):
                continue
            # 模糊匹配不得丢弃名称内部尚未解析的强化标记。
            if base.count("+") != candidate_base.count("+"):
                continue
            similarity = SequenceMatcher(None, base, candidate_base).ratio()
            if card["id"] not in scores or similarity > scores[card["id"]][0]:
                scores[card["id"]] = (similarity, card)
        ranked = sorted(scores.values(), key=lambda item: (-item[0], item[1]["id"]))
        result["candidates"] = [
            {
                "source_id": card["id"],
                "card_id": f"skill_card_{card['id']:04d}",
                "name": card["name"],
                "name_similarity": SequenceMatcher(None, prefix["visible_prefix"], split_variant(card["name"])[0]).ratio() if prefix else score,
            }
            for score, card in ranked[:3]
        ]
        if not ranked:
            result["unknown_reason"] = "no_catalog_variant"
            return result
        best, card = ranked[0]
        margin = best - ranked[1][0] if len(ranked) > 1 else 1.0
        result.update(name_similarity=best, similarity_margin=margin, match_method="exact" if best == 1 else "fuzzy")
        if prefix and prefix["ambiguous"]:
            result.update(match_method="prefix_candidates", unknown_reason="shared_visible_prefix")
            return result
        if not name_match_allowed(recognized, card["name"], ocr_confidence, best, margin):
            result["unknown_reason"] = "low_confidence_or_ambiguous_name"
            return result
        if prefix:
            result.update(
                recognized_name=card["name"],
                match_method="prefix",
                name_similarity=SequenceMatcher(None, prefix["visible_prefix"], split_variant(recognized)[0]).ratio(),
                similarity_margin=prefix["candidate_margin"],
            )
        result.update(
            database_card_id=f"skill_card_{card['id']:04d}",
            source_id=card["id"],
            database_name=card["name"],
            upgraded=bool(card.get("upgraded")),
            plan=card.get("plan"),
            available_customizations=card.get("availableCustomizations"),
            reference_effect_dsl={key: card.get(key, "") for key in ("conditions", "cost", "actions", "limit", "effects")},
        )
        result["base_name"] = card["name"].rstrip("+")
        if plus > 1 or prefix or (allow_overlap and plus == 0):
            reason = "extra_upgrade_markers" if plus > 1 else "upgrade_not_visible_in_overlap"
            result.update(status="base_match_variant_unresolved", unknown_reason=reason, upgrade_status="unresolved", upgraded=None)
        else:
            # 名称匹配只确定库中的变体，不能确认不可见的改造或临时效果。
            result.update(
                status="matched", card_id=result["database_card_id"], upgrade_status="visible_catalog_variant", effect_resolution="catalog_reference_only"
            )
        return result


def clean(text):
    return unicodedata.normalize("NFKC", text).strip()


class RewardCatalog:
    def __init__(self, skills, items_path=None):
        self.skills = skills
        self.index = defaultdict(list)
        self.ids = {}
        self.card_names = {}
        self.item_sha256 = None
        self.item_source = None
        self.item_error = None
        self._skill_cache = {}
        for card in skills.cards:
            self._add("skill_card", card, [card["name"], *card.get("aliases", [])])
        for base, plus, card in skills.entries:
            self.card_names[base + "+" * plus] = card["name"]
            self.index[base + "+" * plus].append(("skill_card", card))
        if items_path and Path(items_path).exists():
            try:
                raw = Path(items_path).read_bytes()
                items = json.loads(raw)
                if items["source"]["revision"] != skills.source["revision"]:
                    raise ValueError("Reward item and skill snapshots differ")
                for table, kind in (("drinks", "drink"), ("p_items", "p_item")):
                    for row in items[table]:
                        names = [row["name"], *row.get("aliases", [])]
                        if row.get("upgraded") and not row["name"].endswith("+"):
                            names.append(row["name"] + "+")
                        self._add(kind, row, names)
                self.item_sha256 = hashlib.sha256(raw).hexdigest()
                self.item_source = items["source"]
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self.item_error = str(exc)
                # 可选道具快照无效时，仍保留技能卡查库能力。
                self.index = defaultdict(list, {key: [(kind, row) for kind, row in values if kind == "skill_card"]
                                                for key, values in self.index.items()})
                self.ids = {key: value for key, value in self.ids.items() if value[0] == "skill_card"}

    @property
    def hashes(self):
        return {"skill_cards": self.skills.sha256, "reward_items": self.item_sha256}

    def _add(self, kind, row, names):
        key = f"{kind}_{row['id']:04d}"
        if key in self.ids:
            raise ValueError("Duplicate reward catalog id")
        self.ids[key] = (kind, row)
        for name in names:
            self.index[normalize_name(name)].append((kind, row))

    @staticmethod
    def dsl(kind, row):
        if kind == "skill_card":
            return {key: row.get(key, "") for key in ("conditions", "cost", "actions", "limit", "effects")}
        return {"conditions": "", "cost": "", "actions": row.get("actions", "") if kind == "drink" else "",
                "limit": "", "effects": row.get("effects", "") if kind == "p_item" else ""}

    def _fuzzy_candidates(self, normalized):
        base, plus = split_variant(normalized)
        scores = {}
        if len(base) < 2:
            return []
        for name, entries in self.index.items():
            candidate_base, candidate_plus = split_variant(name)
            if bool(plus) != bool(candidate_plus) or base.count("+") != candidate_base.count("+"):
                continue
            rows = {(kind, row["id"]): (kind, row) for kind, row in entries}
            if len(rows) > 1:
                rows = {key: value for key, value in rows.items() if bool(value[1].get("upgraded")) == bool(plus)}
            score = SequenceMatcher(None, base, candidate_base).ratio()
            for key, (kind, row) in rows.items():
                if kind != "skill_card" and plus != candidate_plus:
                    continue
                if key not in scores or score > scores[key][0]:
                    scores[key] = (score, name, kind, row)
        return sorted(scores.values(), key=lambda item: (-item[0], item[2], item[3]["id"]))

    def match(self, text, confidence=1.0):
        normalized = normalize_name(text)
        rows = {(kind, row["id"]): (kind, row) for kind, row in self.index.get(normalized, [])}
        match_evidence = {}
        # 部分上游道具变体的名称省略加号，
        # 按明确的 upgraded 字段区分，不能随意选择首个同名条目。
        if len(rows) > 1:
            rows = {key: value for key, value in rows.items() if bool(value[1].get("upgraded")) == normalized.endswith("+")}
        if confidence < .65 or len(rows) != 1:
            if normalized.endswith("++") and normalized.rstrip("+") + "+" in self.card_names:
                identity = self.skills.match(text, confidence)
                if identity.get("database_card_id"):
                    return {**identity, "item_kind": "skill_card", "catalog_id": identity["database_card_id"], "needs_full_ocr": True}
            ranked = self._fuzzy_candidates(normalized) if confidence >= .65 and not rows else []
            if ranked:
                score, name, kind, row = ranked[0]
                margin = score - ranked[1][0] if len(ranked) > 1 else 1.0
                match_evidence = {"name_similarity": score, "similarity_margin": margin, "match_method": "fuzzy",
                                  "candidates": [{"catalog_id": f"{kind}_{row['id']:04d}", "source_id": row["id"],
                                                  "name": row["name"], "name_similarity": score}
                                                 for score, _, kind, row in ranked[:3]]}
            if not ranked or not name_match_allowed(normalized, name, confidence, score, margin):
                return {**self.skills.match(""), "observed_name": text, "normalized_name": normalized, "ocr_confidence": confidence,
                        **match_evidence, "item_kind": "other_or_unknown", "catalog_id": None,
                        "unknown_reason": "low_name_confidence" if confidence < .65 else "ambiguous_or_missing_reward_name",
                        "needs_full_ocr": True}
        else:
            kind, row = next(iter(rows.values()))
        if kind == "skill_card":
            if match_evidence:
                identity = self.skills.match(text, confidence)
            else:
                name = self.card_names.get(normalized, row["name"])
                if name not in self._skill_cache:
                    self._skill_cache[name] = self.skills.match(name)
                identity = copy.deepcopy(self._skill_cache[name])
            identity.update(observed_name=text, normalized_name=normalized, ocr_confidence=confidence)
        else:
            identity = {"status": "matched", "observed_name": text, "normalized_name": normalized, "ocr_confidence": confidence,
                        "database_name": row["name"], "source_id": row["id"], "database_item_id": f"{kind}_{row['id']:04d}",
                        "upgraded": row.get("upgraded", False), "plan": row.get("plan"), "mode": row.get("mode"),
                        "match_method": "exact", "customization": None}
        dsl = self.dsl(kind, row)
        available = bool(dsl["actions"] or dsl["effects"]) and identity["status"] == "matched"
        identity.update(**match_evidence, item_kind=kind, catalog_id=f"{kind}_{row['id']:04d}", reference_effect_dsl=dsl,
                        effective_effect_dsl=copy.deepcopy(dsl) if available else None,
                        effect_resolution="catalog_default" if available else "runtime_variant_unresolved"
                        if identity["status"] != "matched" else "catalog_effect_missing", needs_full_ocr=not available,
                        effective_effect_confirmed=False)
        return identity

    def match_tokens(self, tokens):
        ordered = sorted(tokens, key=lambda token: (token["box"][1], token["box"][0]))
        choices = [self.match(token["text"], token.get("confidence", 0)) for token in ordered]
        if len(ordered) > 1:
            choices.append(self.match("".join(token["text"] for token in ordered), min(t.get("confidence", 0) for t in ordered)))
        matches = {}
        for choice in choices:
            if choice.get("catalog_id"):
                key = choice["catalog_id"]
                if key not in matches or choice.get("name_similarity", 1) > matches[key].get("name_similarity", 1):
                    matches[key] = choice
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(choices) == 1:
            return choices[0]
        return self.match(" ".join(t["text"] for t in ordered), 0)

    def by_id(self, catalog_id):
        found = self.ids.get(catalog_id)
        if not found:
            return self.match("")
        kind, row = found
        name = row["name"]
        if row.get("upgraded") and not name.endswith("+"):
            name += "+"
        identity = self.match(name)
        return identity if identity.get("catalog_id") == catalog_id else self.match("")


def catalog_effects(identity):
    dsl = identity.get("effective_effect_dsl")
    return {"source": "catalog_default", "catalog_id": identity.get("catalog_id"), "dsl": copy.deepcopy(dsl),
            "complete": bool(dsl), "current_effect_confirmed": False,
            "assumption": "standard_catalog_variant; customization_and_temporary_changes_not_confirmed"}
