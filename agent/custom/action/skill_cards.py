"""Skill-card metadata and lightweight hand-card identification helpers."""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image

from utils import logger

REPO_ROOT = Path(__file__).resolve().parents[3]
HASH_SIZE = 16
OCR_MATCH_THRESHOLD = 0.68
TEMPLATE_MATCH_THRESHOLD = 0.74


def _first_existing_path(*paths: Path) -> Path:
    for path in paths:
        if path.exists():
            return path
    return paths[0]


CARD_DATABASE_PATH = _first_existing_path(
    REPO_ROOT / "assets" / "data" / "produce_skill_cards.json",
    REPO_ROOT / "data" / "produce_skill_cards.json",
)
TEMPLATE_DIR = _first_existing_path(
    REPO_ROOT / "assets" / "resource" / "base" / "image" / "produce" / "skill_cards",
    REPO_ROOT / "resource" / "base" / "image" / "produce" / "skill_cards",
)


def _normalize_text(value: str) -> str:
    value = value.lower().replace("＋", "+")
    value = re.sub(r"[\s　・,，.。!！?？:：;；'\"`~〜\-—]+", "", value)
    return value


def _has_name_characters(value: str) -> bool:
    normalized = _normalize_text(value)
    if len(normalized) < 2:
        return False
    return bool(re.search(r"[ぁ-んァ-ン一-龯a-z]", normalized))


@lru_cache(maxsize=1)
def load_skill_cards() -> List[Dict[str, Any]]:
    if not CARD_DATABASE_PATH.exists():
        logger.warning(f"Skill-card database not found: {CARD_DATABASE_PATH}")
        return []
    with CARD_DATABASE_PATH.open("r", encoding="utf-8") as fp:
        database = json.load(fp)
    cards = database.get("cards", [])
    if not isinstance(cards, list):
        return []
    return cards


def find_card_by_id(card_id: str) -> Optional[Dict[str, Any]]:
    for card in load_skill_cards():
        if card.get("id") == card_id:
            return card
    return None


def identify_card_by_name(text: str, threshold: float = OCR_MATCH_THRESHOLD) -> Optional[Tuple[Dict[str, Any], float]]:
    normalized = _normalize_text(text)
    if not normalized:
        return None
    best_card: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for card in load_skill_cards():
        names = [str(card.get("name", "")), str(card.get("name_normalized", ""))]
        for name in names:
            target = _normalize_text(name)
            if not target:
                continue
            score = SequenceMatcher(None, normalized, target).ratio()
            if target in normalized or normalized in target:
                score = max(score, 0.92)
            if score > best_score:
                best_score = score
                best_card = card
    if best_card and best_score >= threshold:
        return best_card, best_score
    return None


def _to_pil(image: Any) -> Optional[Image.Image]:
    if isinstance(image, Image.Image):
        return image
    try:
        return Image.fromarray(image)
    except Exception:
        return None


def _crop(image: Any, box: List[int]) -> Optional[Image.Image]:
    pil_image = _to_pil(image)
    if pil_image is None:
        return None
    x, y, width, height = [int(value) for value in box]
    return pil_image.crop((x, y, x + width, y + height))


def _upscaled_roi_image(image: Any, roi: List[int], scale: int = 3) -> Optional[Tuple[Any, List[int]]]:
    crop = _crop(image, roi)
    if crop is None:
        return None
    resampling = getattr(Image, "Resampling", Image)
    scaled = crop.resize((crop.width * scale, crop.height * scale), resampling.LANCZOS)
    try:
        import numpy as np

        return np.asarray(scaled), [0, 0, scaled.width, scaled.height]
    except Exception:
        return None


def _average_hash(image: Image.Image) -> Tuple[int, ...]:
    resampling = getattr(Image, "Resampling", Image)
    gray = image.convert("L").resize((HASH_SIZE, HASH_SIZE), resampling.LANCZOS)
    pixels = list(gray.getdata())
    average = sum(pixels) / len(pixels)
    return tuple(1 if pixel >= average else 0 for pixel in pixels)


def _hamming(left: Tuple[int, ...], right: Tuple[int, ...]) -> int:
    return sum(1 for left_bit, right_bit in zip(left, right) if left_bit != right_bit)


@lru_cache(maxsize=1)
def _template_hashes() -> List[Dict[str, Any]]:
    templates: List[Dict[str, Any]] = []
    if not TEMPLATE_DIR.exists():
        return templates
    for path in TEMPLATE_DIR.glob("skill_card_*/*.png"):
        card_id = path.parent.name
        variant = path.stem
        try:
            with Image.open(path) as image:
                templates.append({"card_id": card_id, "variant": variant, "hash": _average_hash(image), "path": str(path)})
        except OSError:
            continue
    return templates


def identify_card_by_template(crop: Image.Image, threshold: float = TEMPLATE_MATCH_THRESHOLD) -> Optional[Tuple[Dict[str, Any], str, float]]:
    templates = _template_hashes()
    if not templates:
        return None
    crop_hash = _average_hash(crop)
    best_template: Optional[Dict[str, Any]] = None
    best_score = 0.0
    for template in templates:
        distance = _hamming(crop_hash, template["hash"])
        score = 1.0 - distance / float(HASH_SIZE * HASH_SIZE)
        if score > best_score:
            best_score = score
            best_template = template
    if not best_template or best_score < threshold:
        return None
    card = find_card_by_id(str(best_template["card_id"]))
    if not card:
        return None
    return card, str(best_template["variant"]), best_score


def _ocr_text_from_roi(context: Any, image: Any, roi: List[int]) -> str:
    ocr_image = image
    ocr_roi = roi
    upscaled = _upscaled_roi_image(image, roi)
    if upscaled is not None:
        ocr_image, ocr_roi = upscaled
    try:
        reco_detail = context.run_recognition(
            "ProduceRecognitionSkillCardName",
            ocr_image,
            pipeline_override={"ProduceRecognitionSkillCardName": {"recognition": "OCR", "roi": ocr_roi}},
        )
    except Exception as exc:  # pragma: no cover - Maa runtime guard.
        logger.debug(f"Skill-card OCR failed: {exc}")
        return ""
    if not reco_detail:
        return ""
    results = (getattr(reco_detail, "filtered_results", []) or []) or (getattr(reco_detail, "all_results", []) or [])
    return "".join(str(getattr(item, "text", "")) for item in results)


def _recognize_card_name(context: Any, image: Any, box: List[int]) -> str:
    x, y, width, height = [int(value) for value in box]
    inner_x = x + max(4, int(width * 0.10))
    inner_width = max(20, width - max(8, int(width * 0.20)))
    # The detector box includes the whole hand card. Names are printed on the bottom
    # nameplate; the top area mostly contains cost numbers and icons.
    rois = [
        [inner_x, y + int(height * 0.76), inner_width, max(28, int(height * 0.20))],
        [inner_x, y + int(height * 0.72), inner_width, max(28, int(height * 0.18))],
        [inner_x, y + int(height * 0.82), inner_width, max(24, int(height * 0.14))],
    ]
    texts = []
    for roi in rois:
        text = _ocr_text_from_roi(context, image, roi)
        if text:
            texts.append(text)
        if _has_name_characters(text):
            logger.debug(f"Skill-card OCR text: {text!r} roi={roi}")
            return text
    if texts:
        logger.info(f"Skill-card OCR did not find a usable name: texts={texts} box={box}")
    return ""


def _box_iou(left: List[int], right: List[int]) -> float:
    lx, ly, lw, lh = [int(value) for value in left]
    rx, ry, rw, rh = [int(value) for value in right]
    x1 = max(lx, rx)
    y1 = max(ly, ry)
    x2 = min(lx + lw, rx + rw)
    y2 = min(ly + lh, ry + rh)
    intersection = max(0, x2 - x1) * max(0, y2 - y1)
    union = lw * lh + rw * rh - intersection
    return intersection / union if union else 0.0


def _merge_detection_results(results: Iterable[Any]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    for result in results:
        box = list(getattr(result, "box", [0, 0, 1, 1]))
        label = str(getattr(result, "label", "cards"))
        score = float(getattr(result, "score", 0.0) or 0.0)
        target = None
        for candidate in merged:
            if _box_iou(candidate["box"], box) >= 0.45:
                target = candidate
                break
        if target is None:
            merged.append({"box": box, "labels": {label}, "score": score})
        else:
            old_score = target["score"]
            target["labels"].add(label)
            target["score"] = max(target["score"], score)
            if label == "cards" or score > old_score:
                target["box"] = box
    return sorted(merged, key=lambda item: (item["box"][1], item["box"][0]))


def _card_effect_summary(card: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not card:
        return {}
    effect_dsl = card.get("effect_dsl", {})
    return {
        "conditions": effect_dsl.get("conditions", ""),
        "cost": effect_dsl.get("cost", ""),
        "actions": effect_dsl.get("actions", ""),
        "limit": effect_dsl.get("limit"),
        "effects": effect_dsl.get("effects", ""),
    }


def build_hand_card_candidates(context: Any, image: Any, detection_results: Iterable[Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for slot, detection in enumerate(_merge_detection_results(detection_results), start=1):
        labels = detection["labels"]
        playable = "cards" in labels or "suggestions" in labels
        recommended = "suggestions" in labels
        useless = "useless" in labels and not playable
        candidate_id = f"unknown_slot_{slot}"
        card: Optional[Dict[str, Any]] = None
        variant = "unknown"
        confidence = float(detection.get("score", 0.0))
        name_text = _recognize_card_name(context, image, detection["box"])
        name_match = identify_card_by_name(name_text)
        if name_match:
            card, confidence = name_match
            variant = str(card.get("variant", "unknown"))
            candidate_id = f"slot_{slot}_{card.get('id')}"
            logger.info(f"Skill-card slot {slot}: matched {card.get('name')} score={confidence:.2f}")
        else:
            cropped = _crop(image, detection["box"])
            template_match = identify_card_by_template(cropped) if cropped is not None else None
            if template_match:
                card, variant, confidence = template_match
                candidate_id = f"slot_{slot}_{card.get('id')}"
                logger.info(f"Skill-card slot {slot}: matched template {card.get('name')} score={confidence:.2f}")
            else:
                logger.info(f"Skill-card slot {slot}: unknown text={name_text!r} box={detection['box']}")
        candidates.append(
            {
                "candidate_id": candidate_id,
                "card_id": card.get("id") if card else None,
                "variant": variant,
                "slot": slot,
                "box": detection["box"],
                "name": card.get("name") if card else name_text,
                "effect": _card_effect_summary(card),
                "confidence": round(confidence, 4),
                "recommended": recommended,
                "playable": playable,
                "useless": useless,
            }
        )
    if candidates and all(candidate.get("card_id") is None for candidate in candidates):
        logger.warning("All skill-card candidates are unknown; LLM card choice will have no effect data")
    return candidates
