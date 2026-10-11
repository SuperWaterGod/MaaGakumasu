"""Conservative OCR-to-catalog matching. Preserve upgrade ambiguity and raw DSL."""

import json
import hashlib
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


def normalize_name(text):
    text = unicodedata.normalize("NFKC", text).replace("＋", "+")
    return "".join(c for c in text if c.isalnum() or c == "+").casefold()


def split_variant(text):
    normalized = normalize_name(text)
    base = normalized.rstrip("+")
    return base, len(normalized) - len(base)


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
        """Trim short adjacent-card suffixes only after a complete catalog name."""
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
        """Rank visible prefixes by coverage; normal/upgraded records share one name."""
        base, plus = split_variant(normalized)
        if len(base) < 3 or "+" in base:
            return None
        names = {candidate for candidate, _, _ in self.entries if candidate.startswith(base) and candidate != base}
        ranked = sorted(((len(base) / len(name), name) for name in names), reverse=True)
        if not ranked:
            return None
        coverage, name = ranked[0]
        margin = coverage - ranked[1][0] if len(ranked) > 1 else 1.0
        # A shorter catalog name is closer by length, but not stronger evidence when
        # another distinct card has the very same visible prefix.
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
            # A visible + selects an upgraded record. ++ retains an unresolved runtime variant.
            if bool(plus) != bool(candidate_plus):
                continue
            # A fuzzy match must not erase unparsed interior upgrade markers.
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
        if ocr_confidence < 0.65 or best < 0.90 or margin < 0.08:
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
            # Name matching identifies the catalog variant, not invisible customizations or temporary buffs.
            result.update(
                status="matched", card_id=result["database_card_id"], upgrade_status="visible_catalog_variant", effect_resolution="catalog_reference_only"
            )
        return result
