"""Read the battle HUD with native Maa OCR and data-driven green-mask templates."""

import re
import json
import hashlib
import unicodedata
from pathlib import Path

import numpy as np

STATE_FIELDS = ("turns_remaining", "score", "stamina", "genki", "attribute", "attribute_multiplier_percent")
ANOMALY_FIELDS = (
    "stance",
    "stance_stage",
    "full_power_charge",
    "next_stance",
    "transition_after_turns",
    "held_cards_count",
    "held_cards_capacity",
)
STANCE_NAMES = {"strength": "强气", "preservation": "温存", "fullPower": "全力", "none": "通常"}


def unknown_field(reason="not_observed", roi=None, raw=None):
    return {"value": None, "status": "unknown", "reason": reason, "roi": roi, "raw_ocr": raw or [], "confidence": None}


def number_field(raw, roi, *, maximum, percent=False, minimum_confidence=0.70):
    values = []
    for item in raw:
        text = unicodedata.normalize("NFKC", item["text"]).strip()
        pattern = r"\d[\d,]*\s*%" if percent else r"[(), .]*\d[\d,]*[(), .]*"
        if not re.fullmatch(pattern, text) or item["confidence"] < minimum_confidence:
            continue
        value = int(re.sub(r"\D", "", text))
        if value <= maximum:
            values.append((value, item["confidence"]))
    if len({value for value, _ in values}) != 1:
        return unknown_field("missing_or_ambiguous_number", roi, raw)
    value, confidence = max(values, key=lambda item: item[1])
    return {"value": value, "status": "observed", "confidence": confidence, "roi": roi, "raw_ocr": raw}


def buff_number(raw, roi, format_name):
    if format_name in ("integer", "percent"):
        return number_field(raw, roi, maximum=9999, percent=format_name == "percent")
    # Maa occasionally reads the first katakana タ as the visually similar 夕.
    suffix = {"turns": r"(?:ターン|夕ーン|回合)", "times": r"(?:回|次)", "turns_after": r"(?:ターン|夕ーン)後|回合后|回合後"}[format_name]
    accepted = []
    for item in raw:
        text = unicodedata.normalize("NFKC", item["text"]).strip()
        match = re.fullmatch(r"(\d{1,4})\s*(?:" + suffix + ")", text)
        if match:
            accepted.append({**item, "text": match.group(1)})
    result = number_field(accepted, roi, maximum=9999)
    result["raw_ocr"] = raw
    return result


def number_views(views):
    """Use readable numeric views while preserving disagreements as unknown."""
    values = {view["value"] for view in views if view["value"] is not None}
    if len(values) == 1:
        reading = max((view for view in views if view["value"] is not None), key=lambda view: view["confidence"])
        return {**reading, "value_views": views}
    reason = "conflicting_number_views" if values else "missing_or_ambiguous_number"
    reading = unknown_field(reason, views[0]["roi"], [item for view in views for item in view["raw_ocr"]])
    return {**reading, "value_views": views}


def overlap_ratio(left, right):
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[0] + left[2], right[0] + right[2]), min(left[1] + left[3], right[1] + right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    union = left[2] * left[3] + right[2] * right[3] - intersection
    return intersection / union if union else 0


def player_score_roi(rgb):
    """Locate the NIA player's wide white rank frame, independent of color.

    Opponents can use colored or familiar portraits. Only a unique pair of
    frame sides is accepted; colors and faces never select the player.
    """
    frame = rgb[10:156, 216:720].astype(int)
    white = (frame.min(axis=2) > 225) & (frame.max(axis=2) - frame.min(axis=2) < 30)
    columns = np.flatnonzero(white.sum(axis=0) >= 120) + 216
    groups = np.split(columns, np.flatnonzero(np.diff(columns) > 2) + 1) if len(columns) else []
    pairs = [
        (int(left[0]), int(right[0])) for index, left in enumerate(groups) for right in groups[index + 1 :] if 116 <= right[0] - left[0] <= 126
    ]
    if len(pairs) == 1:
        left, right = pairs[0]
        left, right = max(216, left - 4), min(720, right + 8)
        return [left, 124, right - left, 49]
    return None


def held_card_fields(raw, roi):
    """Parse only an explicit visible held-card counter, never infer zero."""
    pairs = []
    for item in raw:
        text = unicodedata.normalize("NFKC", item["text"]).strip()
        match = re.fullmatch(r"保留\s*(\d{1,2})\s*/\s*(\d{1,2})", text)
        if match and item["confidence"] >= 0.70:
            count, capacity = map(int, match.groups())
            if 0 <= count <= capacity <= 99:
                pairs.append((count, capacity, item["confidence"]))
    if len({(count, capacity) for count, capacity, _ in pairs}) != 1:
        return [unknown_field("missing_or_ambiguous_held_counter", roi, raw) for _ in range(2)]
    count, capacity, confidence = max(pairs, key=lambda pair: pair[2])
    return [{"value": value, "status": "observed", "roi": roi, "raw_ocr": raw, "confidence": confidence} for value in (count, capacity)]


class BattleStateReader:
    def __init__(self, registry: Path, read_ocr, match_templates=None, image_root=None):
        self.read_ocr = read_ocr
        self.match_templates = match_templates
        self.template_sha256 = None
        self.template_error = None
        self.templates = []
        self.ambiguity_margin = 0.025
        try:
            content = registry.read_bytes()
            data = json.loads(content)
            self.ambiguity_margin = float(data.get("ambiguity_margin", 0.025))
            digest = hashlib.sha256(content)
            problems = []
            entries = [
                (kind, entry)
                for group, kind in (("buffs", "buff"), ("stances", "stance"), ("stance_previews", "stance_preview"))
                for entry in data.get(group, [])
            ]
            for kind, definition in entries:
                entry = {**definition, "kind": kind}
                if entry.get("enabled") is False:
                    continue
                if entry.get("reading", {}).get("format", "integer") not in ("integer", "percent", "turns", "times", "turns_after"):
                    raise ValueError("Unsupported Buff value format")
                if image_root:
                    root = Path(image_root).resolve()
                    template = (root / entry["template"]).resolve()
                    if not template.is_relative_to(root):
                        raise ValueError("Template escaped image directory")
                    if not template.is_file():
                        problems.append("Missing template: " + entry["template"])
                        continue
                    raw = template.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != entry["template_sha256"]:
                        problems.append("Template checksum mismatch: " + entry["template"])
                        continue
                    digest.update(raw)
                self.templates.append(entry)
            self.template_sha256 = digest.hexdigest()
            if problems:
                self.template_error = "; ".join(problems)
            if not match_templates:
                self.template_error = "Native TemplateMatch callback unavailable"
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.templates = []
            self.template_error = str(exc)

    def _ocr(self, array, roi, errors):
        try:
            return self.read_ocr(array, roi)
        except Exception as exc:
            errors.append({"roi": roi, "error": str(exc)})
            return []

    def _reading(self, array, box, spec, errors):
        dx, dy, width, height = spec["roi_offset"]
        left, top = max(0, box[0] + dx), max(0, box[1] + dy)
        roi = [left, top, min(width, 720 - left), min(height, 1280 - top)]
        raw = self._ocr(array, roi, errors)
        return buff_number(raw, roi, spec["format"])

    def _matches(self, array, kind, errors):
        hits = []
        if self.match_templates:
            for entry in self.templates:
                if entry["kind"] != kind:
                    continue
                try:
                    for hit in self.match_templates(array, entry):
                        if hit["score"] >= entry["threshold"] - self.ambiguity_margin:
                            hits.append({**hit, "definition": entry})
                except Exception as exc:
                    errors.append({"template": entry["template"], "error": str(exc)})
        groups = []
        for hit in sorted(hits, key=lambda item: -item["score"]):
            group = next((g for g in groups if overlap_ratio(g[0]["box"], hit["box"]) >= 0.45), None)
            if group is None:
                groups.append([hit])
            else:
                group.append(hit)
        return sorted(groups, key=lambda g: g[0]["box"][1])

    @staticmethod
    def _icon_evidence(best, group):
        entry = best["definition"]
        return {
            "backend": "MaaFramework.TemplateMatch",
            "template_id": entry["id"],
            "template": entry["template"],
            "score": best["score"],
            "threshold": entry["threshold"],
            "method": entry["method"],
            "green_mask": True,
            "candidates": [{"id": hit["definition"]["id"], "score": hit["score"]} for hit in group[:3]],
        }

    def _buffs(self, array, rgb, errors):
        buffs, unknown = [], []
        for group in self._matches(array, "buff", errors):
            original_group = group
            unit_evidence = None
            family = group[0]["definition"].get("value_family")
            relatives = [hit for hit in group if family and hit["definition"].get("value_family") == family]
            # Only explicitly registered glyph relatives can be separated by
            # value units. Unrelated icon rivals retain their score competition.
            if len({hit["definition"]["reading"]["format"] for hit in relatives}) > 1:
                readings = []
                for hit in relatives:
                    spec = hit["definition"]["reading"]
                    views = [self._reading(array, hit["box"], spec, errors)]
                    if spec.get("second_roi_offset"):
                        views.append(self._reading(array, hit["box"], {**spec, "roi_offset": spec["second_roi_offset"]}, errors))
                    readings.extend({"format": spec["format"], "reading": view} for view in views)
                observed_formats = {view["format"] for view in readings if view["reading"]["value"] is not None}
                if len(observed_formats) == 1:
                    value_format = observed_formats.pop()
                    group = [
                        hit
                        for hit in group
                        if hit["definition"].get("value_family") != family or hit["definition"]["reading"]["format"] == value_format
                    ]
                    unit_evidence = {"family": family, "format": value_format, "value_views": readings}
            best = group[0]
            entry, box = best["definition"], best["box"]
            if best["score"] < entry["threshold"]:
                continue
            rival = next((hit for hit in group if hit["definition"]["key"] != entry["key"]), None)
            evidence = self._icon_evidence(best, original_group)
            if unit_evidence:
                evidence["value_format_disambiguation"] = unit_evidence
            margin = (
                max(entry.get("ambiguity_margin", self.ambiguity_margin), rival["definition"].get("ambiguity_margin", self.ambiguity_margin))
                if rival
                else self.ambiguity_margin
            )
            if rival and best["score"] - rival["score"] < margin:
                unknown.append({"box": box, "icon_evidence": evidence, "value": None, "status": "unknown", "reason": "ambiguous_templates"})
                continue
            reading = self._reading(array, box, entry["reading"], errors)
            views = [reading]
            second_offset = entry["reading"].get("second_roi_offset")
            if second_offset:
                views.append(self._reading(array, box, {**entry["reading"], "roi_offset": second_offset}, errors))
            observed_values = {view["value"] for view in views if view["value"] is not None}
            incompatible_unit = {
                "integer": r"\d\s*(?:%|ターン|夕ーン|回合|回|次)",
                "turns": r"\d\s*%",
            }.get(entry["reading"]["format"])
            if incompatible_unit and any(
                re.search(incompatible_unit, unicodedata.normalize("NFKC", item["text"])) for view in views for item in view["raw_ocr"]
            ):
                # A modifier may reuse the same base glyph. Its explicit unit is
                # evidence against interpreting it as a base stack/duration.
                unknown.append(
                    {
                        "box": box,
                        "icon_evidence": evidence,
                        "candidate_key": entry["key"],
                        "value": None,
                        "status": "unknown",
                        "reason": "value_unit_conflict",
                        "value_views": views,
                    }
                )
                continue
            conflict = len(observed_values) > 1
            if not conflict:
                reading = next((view for view in views if view["value"] is not None), reading)
            value = reading["value"] if not conflict else None
            duration = value if entry["unit"] == "turns" else None
            duration_evidence = None
            if entry.get("duration_reading"):
                duration_evidence = self._reading(array, box, entry["duration_reading"], errors)
                duration = duration_evidence["value"]
            buffs.append(
                {
                    "box": box,
                    "key": entry["key"],
                    "name": entry["name"],
                    "unit": entry["unit"],
                    "value": value,
                    "duration": duration,
                    "duration_evidence": duration_evidence,
                    "status": "observed" if value is not None else "icon_only",
                    "unknown_reason": "conflicting_value_views" if conflict else ("value_not_observed" if value is None else None),
                    "icon_evidence": evidence,
                    "value_views": views,
                    "raw_ocr": reading["raw_ocr"],
                    "ocr_confidence": reading["confidence"],
                }
            )
        # Retain unclassified regions for new-flow material collection without
        # asserting that every sampled region really contains a Buff.
        for slot in range(8):
            cy = round(263 + slot * 61.4)
            box = [14, cy - 30, 60, 60]
            if not any(overlap_ratio(box, b["box"]) >= 0.45 for b in [*buffs, *unknown]):
                unknown.append({"box": box, "status": "candidate_region", "reason": "no_registered_template", "value": None})
        return buffs, unknown

    def _single_icon(self, array, kind, errors):
        groups = self._matches(array, kind, errors)
        qualified = [group for group in groups if group[0]["score"] >= group[0]["definition"]["threshold"]]
        if len(qualified) != 1:
            return None, unknown_field("no_unique_" + kind + "_icon")
        group = qualified[0]
        best = group[0]
        evidence = self._icon_evidence(best, group)
        rival = next((hit for hit in group if hit["definition"]["id"] != best["definition"]["id"]), None)
        if rival and best["score"] - rival["score"] < self.ambiguity_margin:
            return None, {**unknown_field("ambiguous_templates"), "icon_evidence": evidence}
        return best, {"value": best["definition"]["key"], "status": "observed", "box": best["box"], "icon_evidence": evidence}

    def _numeric_views(self, array, box, spec, errors):
        views = [self._reading(array, box, spec, errors)]
        if spec.get("second_roi_offset"):
            views.append(self._reading(array, box, {**spec, "roi_offset": spec["second_roi_offset"]}, errors))
        return number_views(views)

    def _anomaly(self, array, errors, buffs=()):
        result = {key: unknown_field() for key in ANOMALY_FIELDS}
        held_views = [held_card_fields(self._ocr(array, roi, errors), roi) for roi in ([503, 1140, 124, 35], [514, 1146, 112, 29])]
        result["held_cards_count"] = number_views([view[0] for view in held_views])
        result["held_cards_capacity"] = number_views([view[1] for view in held_views])
        result["pointer_condition"] = {"value": None, "status": "unknown", "raw_ocr": []}
        current, result["stance"] = self._single_icon(array, "stance", errors)
        result["hud_present"] = current is not None
        if current is None:
            return result
        entry, box = current["definition"], current["box"]
        competing = next(
            (
                buff
                for buff in buffs
                if overlap_ratio(box, buff["box"]) >= 0.45 and buff["icon_evidence"]["score"] >= current["score"] - self.ambiguity_margin
            ),
            None,
        )
        if competing:
            result["stance"] = {
                **unknown_field("conflicts_with_buff_icon"),
                "icon_evidence": result["stance"]["icon_evidence"],
                "competing_buff": competing["icon_evidence"],
            }
            result["hud_present"] = False
            return result
        upcoming, result["next_stance"] = self._single_icon(array, "stance_preview", errors)
        if entry["key"] == "none" and upcoming is None:
            # A gray neutral badge alone can resemble a faded ordinary Buff.
            # Confirm the independent non凡 preview before claiming this layout.
            result["stance"] = {**unknown_field("neutral_icon_without_anomaly_context"), "icon_evidence": result["stance"]["icon_evidence"]}
            result["hud_present"] = False
            return result
        if entry.get("reading"):
            stage = self._numeric_views(array, box, entry["reading"], errors)
            result["stance_stage"] = stage if stage["value"] in (1, 2) else {**stage, "value": None, "status": "unknown"}
        result["full_power_charge"] = self._numeric_views(array, box, entry["charge_reading"], errors)
        if upcoming:
            result["transition_after_turns"] = self._numeric_views(array, upcoming["box"], upcoming["definition"]["reading"], errors)
        # This line describes a pointer condition/threshold. It is not a counter
        # of actual cumulative charge or actual stance changes.
        roi = [104, 201, 210, 42]
        raw = self._ocr(array, roi, errors)
        texts = [item["text"] for item in raw if item["confidence"] >= 0.70 and re.search(r"変更|变更|變更|累計|累计|累計", item["text"])]
        result["pointer_condition"] = {
            "value": " ".join(texts) or None,
            "status": "observed" if texts else "unknown",
            "roi": roi,
            "raw_ocr": raw,
            "interpretation": "visible_condition_text_only",
        }
        return result

    def read(self, array):
        errors = []
        rgb = array[..., ::-1]
        fields = {key: unknown_field() for key in STATE_FIELDS}
        fields["turns_remaining"] = number_views(
            [number_field(self._ocr(array, roi, errors), roi, maximum=99) for roi in ([14, 64, 88, 64], [32, 69, 60, 46])]
        )
        for key, roi, maximum in [
            ("stamina", [563, 207, 86, 40], 999),
        ]:
            fields[key] = number_field(self._ocr(array, roi, errors), roi, maximum=maximum)
        fields["genki"] = number_views(
            [number_field(self._ocr(array, roi, errors), roi, maximum=99999) for roi in ([643, 170, 72, 43], [648, 172, 67, 40])]
        )
        roi = [106, 44, 115, 81]
        raw = self._ocr(array, roi, errors)
        fields["attribute_multiplier_percent"] = number_field(raw, roi, maximum=99999, percent=True)
        attributes = []
        for item in raw:
            text = unicodedata.normalize("NFKC", item["text"]).casefold()
            for value, tokens in {
                "Vo": ("ボーカル", "声乐", "聲樂", "vocal"),
                "Da": ("ダンス", "舞蹈", "dance"),
                "Vi": ("ビジュアル", "形象", "visual"),
            }.items():
                if item["confidence"] >= 0.70 and any(token in text for token in tokens):
                    attributes.append((value, item["confidence"]))
        if len({value for value, _ in attributes}) == 1:
            value, confidence = max(attributes, key=lambda item: item[1])
            fields["attribute"] = {"value": value, "status": "observed", "roi": roi, "raw_ocr": raw, "confidence": confidence}
        present = sum(fields[key]["value"] is not None for key in ("turns_remaining", "stamina", "genki", "attribute_multiplier_percent")) >= 3
        state = {
            "schema_version": 1,
            "hud_present": present,
            "fields": fields,
            "buffs": [],
            "unknown_buffs": [],
            "buffs_complete": False,
            "turn_number": None,
            "card_uses_remaining": None,
            "stance": None,
            "errors": errors,
            "icon_templates_sha256": self.template_sha256,
        }
        if self.template_error:
            state["template_error"] = self.template_error
        if not present:
            state["fields"] = {key: unknown_field("battle_hud_not_confirmed", field["roi"], field["raw_ocr"]) for key, field in fields.items()}
            return state
        score_roi = player_score_roi(rgb)
        if score_roi:
            state["fields"]["score"] = number_field(self._ocr(array, score_roi, errors), score_roi, maximum=999999999)
            state["score_locator"] = "unique_wide_white_player_rank_frame"
        else:
            state["fields"]["score"] = unknown_field("unsupported_or_ambiguous_score_layout")
        state["buffs"], state["unknown_buffs"] = self._buffs(array, rgb, errors)
        state["anomaly"] = self._anomaly(array, errors, state["buffs"])
        state["stance"] = state["anomaly"]["stance"]["value"]
        # Side lists can be scrolled or abbreviated. Absence is never an implicit zero.
        state["buffs_incomplete_reason"] = "registered_visible_templates_only; hidden, faded and unregistered icons may be unparsed"
        return state


def state_changes(before, after):
    changes = {}
    if not before or not after or not before.get("hud_present") or not after.get("hud_present"):
        return changes
    for key in STATE_FIELDS:
        old, new = before["fields"][key]["value"], after["fields"][key]["value"]
        if old is not None and new is not None and old != new:
            changes[key] = {"before": old, "after": new}
    for key in ANOMALY_FIELDS:
        old = before.get("anomaly", {}).get(key, {}).get("value")
        new = after.get("anomaly", {}).get(key, {}).get("value")
        if old is not None and new is not None and old != new:
            changes["anomaly_" + key] = {"before": old, "after": new}
    old_buffs = {b["key"]: b["value"] for b in before["buffs"] if sum(other["key"] == b["key"] for other in before["buffs"]) == 1}
    for buff in after["buffs"]:
        if sum(other["key"] == buff["key"] for other in after["buffs"]) != 1:
            continue
        old = old_buffs.get(buff["key"])
        if old is not None and buff["value"] is not None and old != buff["value"]:
            changes[buff["key"]] = {"before": old, "after": buff["value"]}
    return changes
