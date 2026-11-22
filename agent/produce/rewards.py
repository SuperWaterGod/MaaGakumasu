"""奖励整页图标识别与历史奖励记录读取。"""

import re
import copy
import time
import hashlib
from collections import OrderedDict

import numpy as np
from utils.image_io import bgr_image
from utils.task_control import wait_running, click_running

from .icons import icons_for_capture
from .state import PRODUCE_FRAME_SIZE, scale_box, normalized_image
from .catalog import RewardCatalog, clean, catalog_effects


def reward_kind(text):
    """复用页面标题分类；未确认类别时不排除任何模板。"""
    text = clean(text)
    if re.search(r"P[・･ ·]?ドリンク|饮料|飲料", text):
        return "drink"
    if re.search(r"スキルカード|技能卡", text):
        return "skill_card"
    if re.search(r"P[・･ ·]?アイテム|P道具|P 道具", text):
        return "p_item"
    return None


ROW_ROI = (55, 790, 610, 185)
DETAIL_ROIS = {"name": [90, 487, 480, 55], "effects": [90, 545, 550, 233], "cost": [570, 485, 70, 65]}


def rgb_image(image):
    return image[:, :, ::-1] if isinstance(image, np.ndarray) and image.ndim == 3 and image.shape[2] == 3 else None


def orange_mask(array):
    return (array[:, :, 0] > 230) & (array[:, :, 1] > 105) & (array[:, :, 1] < 200) & (array[:, :, 2] < 135)


def components(mask):
    mask = mask.copy()
    height, width = mask.shape
    for y, x in zip(*np.nonzero(mask)):
        if not mask[y, x]:
            continue
        stack = [(y, x)]
        mask[y, x] = False
        left = right = x
        top = bottom = y
        count = 0
        while stack:
            cy, cx = stack.pop()
            left, right = min(left, cx), max(right, cx)
            top, bottom = min(top, cy), max(bottom, cy)
            count += 1
            for ny, nx in ((cy - 1, cx), (cy + 1, cx), (cy, cx - 1), (cy, cx + 1)):
                if 0 <= ny < height and 0 <= nx < width and mask[ny, nx]:
                    mask[ny, nx] = False
                    stack.append((ny, nx))
        yield [left, top, right - left + 1, bottom - top + 1], count


def selection_pixels(array, box):
    x, y, w, h = box
    # 只读取图标外侧的选中括号，排除卡面、消耗角标和文字。
    orange = orange_mask(array)
    return int(sum(region.sum() for region in (
        orange[y + 10:y + h - 10, x - 13:x - 2],
        orange[y + 10:y + h - 10, x + w + 2:x + w + 13],
        orange[y - 13:y - 2, x + 10:x + w - 10],
        orange[y + h + 2:y + h + 13, x + 10:x + w - 10],
    )))


def reward_layout(image):
    array = rgb_image(image)
    if array is None or array.shape != (1280, 720, 3):
        return {"status": "unsupported_size", "candidates": [], "selected_index": None}
    left, top, width, height = ROW_ROI
    crop = array[top:top + height, left:left + width].astype(np.int16)
    gray = (crop.max(axis=2) < 175) & (crop.min(axis=2) > 30) & (np.ptp(crop, axis=2) < 65)
    seeds = []
    for (x, y, w, h), count in components(gray):
        if 120 <= w <= 137 and 120 <= h <= 137 and count >= 500:
            seeds.append([int(x) + left, int(y) + top, int(w), int(h)])
    seeds.sort()
    if len(seeds) < 2 or len(seeds) > 4 or max(b[1] for b in seeds) - min(b[1] for b in seeds) > 4:
        return {"status": "ambiguous_frames", "candidates": [], "selected_index": None}
    w, h = (int(np.median([b[i] for b in seeds])) for i in (2, 3))
    y = int(np.median([b[1] for b in seeds]))
    gaps = [b[0] - a[0] for a, b in zip(seeds, seeds[1:])]
    # 选中项的边框可能断开，两个完整边框之间会隔着一张候选。
    # 从间隔推回唯一的网格步长，随后仍逐格核验边框与选中括号。
    centers = [box[0] + box[2] / 2 for box in seeds]
    center_gaps = [b - a for a, b in zip(centers, centers[1:])]
    pitches = {round(min(center_gaps) / divisor) for divisor in range(1, 4)}
    pitches = {round(float(np.median([gap / max(1, round(gap / pitch)) for gap in center_gaps])))
               for pitch in pitches if w + 4 <= pitch <= w + 24}
    pitches = [pitch for pitch in pitches if w + 4 <= pitch <= w + 24
               and all(abs(gap - round(gap / pitch) * pitch) <= 4 for gap in center_gaps)]
    if len(pitches) != 1:
        return {"status": "ambiguous_spacing", "candidates": [], "selected_index": None,
                "detected_frames": seeds, "frame_gaps": gaps}
    pitch = pitches[0]
    # 只补齐经过画面核验的网格位置；角标可能将选中卡牌的边框分成多段，
    # 少了一段边框不代表少了一张候选牌。
    boxes = []
    for step in range(-4, 5):
        x = seeds[0][0] + step * pitch
        if x < left or x + w > left + width:
            continue
        dx, dy = x - left, y - top
        sides = [
            gray[dy + 12:dy + h - 12, dx:dx + 4].any(axis=1).mean(),
            gray[dy + 12:dy + h - 12, dx + w - 4:dx + w].any(axis=1).mean(),
            gray[dy:dy + 4, dx + 12:dx + w - 12].any(axis=0).mean(),
            gray[dy + h - 4:dy + h, dx + 12:dx + w - 12].any(axis=0).mean(),
        ]
        if sum(value > .55 for value in sides) >= 3 or (max(sides) > .55 and selection_pixels(array, [x, y, w, h]) >= 600):
            boxes.append([x, y, w, h])
    if not 2 <= len(boxes) <= 4 or any(not any(abs(seed[0] + seed[2] / 2 - box[0] - box[2] / 2) <= 3 for box in boxes) for seed in seeds):
        return {"status": "incomplete_frames", "candidates": [], "selected_index": None}
    scores = [selection_pixels(array, box) for box in boxes]
    selected = [i for i, score in enumerate(scores) if score >= 600]
    return {
        "status": "ready" if len(selected) == 1 else "selection_unconfirmed",
        "candidates": [{"candidate_id": f"reward_{i + 1}", "box": box, "highlight_pixels": scores[i]} for i, box in enumerate(boxes)],
        "selected_index": selected[0] if len(selected) == 1 else None,
        "detector": "single_row_frame_and_outer_orange_brackets_v1",
    }


def shop_exit_visible(context, image):
    """核验货架的退出按钮，排除挡住底栏的成交弹层。"""
    node = "ProduceShoppingExit"
    detail = context.run_recognition(node, image, pipeline_override={
        node: {"roi": [560, 1035, 160, 105], "threshold": 0.9},
    })
    return bool(detail and detail.hit)


def observe_reward_page(capture, context, image, item_kind):
    """用总览匹配奖励图标，补读未匹配候选的详情名称。"""
    started = time.monotonic()
    try:
        analysis_image = normalized_image(image)
    except ValueError:
        layout = {"status": "unsupported_size", "candidates": [], "selected_index": None}
    else:
        layout = reward_layout(analysis_image)
    if not layout["candidates"]:
        if capture.reward_preview_enabled:
            capture.record("reward_layout_unavailable", image=image, context=context, save_frame=True,
                           reward_kind=item_kind, layout=layout)
        reason = "截图尺寸不支持" if layout["status"] == "unsupported_size" else "奖励候选位置未确认"
        return {"group_id": None, "candidates": [], "fallback_reason": reason}

    reader = capture.reward_reader
    icons = reader.icons if reader else icons_for_capture(capture.directory, icon_resources=capture.icon_resources)
    matches = [icons.lookup(analysis_image, candidate["box"], item_kind, allow_unverified_variant=True) if icons else None
               for candidate in layout["candidates"]]
    source_size = image.shape[1::-1]
    if source_size != PRODUCE_FRAME_SIZE:
        for candidate in layout["candidates"]:
            candidate["analysis_box"] = candidate["box"]
            candidate["box"] = scale_box(candidate["analysis_box"], PRODUCE_FRAME_SIZE, source_size)
    candidates = [{**candidate, "catalog_id": match["catalog_id"] if match else None}
                  for candidate, match in zip(layout["candidates"], matches)]
    page_id = None
    if capture.reward_preview_enabled:
        page_id = capture.record(
            "reward_page_start", image=image, context=context, save_frame=True, layout=layout,
            reward_kind=item_kind, candidate_source="page_icons", selection_order="observe_then_choose",
        )
    previews = []
    preview_clicks = 0
    for index, (candidate, match) in enumerate(zip(candidates, matches)):
        if context is not None and context.tasker.stopping:
            break
        if match and match.get("base_name"):
            candidate["base_name"] = match["base_name"]
            if match.get("display_name"):
                candidate["display_name"] = match["display_name"]
        frame = image
        evidence = {"observation_method": "upstream_icon" if match else "unmatched_icon",
                    "frame_reference": page_id, "cached_identity": match}
        if not candidate["catalog_id"] and context is not None and not context.tasker.stopping:
            if index == layout.get("selected_index") and not preview_clicks:
                detail_frame = image
            else:
                preview_clicks += 1
                detail_frame = open_reward_detail(context, candidate["box"], candidate.get("analysis_box"))
            if detail_frame is not None:
                tokens = read_reward_name(context, detail_frame)
                confirmed = False
                if reader:
                    identity = reader.catalog.match_tokens(tokens)
                    if identity["status"] == "matched" and item_kind in (None, identity["item_kind"]):
                        candidate["catalog_id"] = identity["catalog_id"]
                        candidate.pop("base_name", None)
                        candidate.pop("display_name", None)
                        confirmed = True
                if confirmed or not match:
                    frame = detail_frame
                    evidence = {"observation_method": "detail_ocr", "name_tokens": tokens, "save_frame": frame is not image,
                                "frame_reference": page_id if frame is image else None}
        if page_id:
            event_id = capture.record(
                "reward_candidate_preview", image=frame, context=context, reward_page_id=page_id, reward_kind=item_kind,
                candidate=candidate, **evidence,
            )
            if event_id:
                previews.append(event_id)
    if page_id:
        complete = len(previews) == len(candidates)
        capture.record(
            "reward_page_end", context=context, reward_page_id=page_id, reward_kind=item_kind, previews=previews,
            candidate_previews_complete=complete, safe_to_receive=context is None or not context.tasker.stopping,
            cache_hits=sum(bool(match and match["catalog_id"]) for match in matches), preview_clicks=preview_clicks,
            expected_count=len(candidates), elapsed_seconds=round(time.monotonic() - started, 3),
        )
    return {"group_id": page_id, "candidates": candidates}


def open_reward_detail(context, box, analysis_box=None):
    """点开一项并读取详情画面，选中括号用于关联当前候选。"""
    x, y, width, height = box
    tasker = context.tasker
    if not click_running(tasker, x + width // 2, y + height // 2) or not wait_running(tasker, .3):
        return None
    frame = tasker.controller.post_screencap().wait().get()
    try:
        array = rgb_image(normalized_image(frame))
    except ValueError:
        return None
    return frame if selection_pixels(array, analysis_box or box) >= 600 else None


def read_reward_name(context, image):
    from maa.pipeline import JOCR, JRecognitionType

    detail = context.run_recognition_direct(JRecognitionType.OCR, JOCR(roi=DETAIL_ROIS["name"]), normalized_image(image))
    return [{"text": row.text, "confidence": row.score, "box": list(row.box)} for row in detail.all_results] if detail else []


class RewardReader:
    def __init__(self, skills, items_path, directory=None, recognize=None, *, icon_resources=None):
        self.catalog = RewardCatalog(skills, items_path)
        self.icons = icons_for_capture(directory, self.catalog.hashes, recognize, icon_resources=icon_resources) if directory else None
        self.details = OrderedDict()
        self.counts = {"name_ocr": 0, "detail_cache": 0, "upstream_icon": 0}

    def read(self, image, event, read_roi):
        if event.get("observation_method") == "detail_ocr":
            self.counts["name_ocr"] += 1
            return reward_preview_observation(
                {"name": event["name_tokens"]}, self.catalog.skills, event, self.catalog,
                {"method": "name_then_catalog", "full_body_ocr": False, "source_event_id": event["event_id"]},
            )
        if event.get("observation_method") == "unmatched_icon":
            return reward_preview_observation({}, self.catalog.skills, event, self.catalog, {"method": "unmatched_icon"})
        cached_icon = event.get("cached_identity")
        if event.get("observation_method") in {"icon_cache", "upstream_icon"}:
            candidate = event["candidate"]
            x, y, width, height = candidate.get("analysis_box") or candidate["box"]
            digest = hashlib.sha256(bgr_image(image[y:y + height, x:x + width]).tobytes()).hexdigest()
            match = cached_icon
            partial = match and match.get("status") == "base_match_variant_unresolved"
            if (not match or match.get("query_icon_sha256") != digest
                    or match.get("catalog_hashes") != self.catalog.hashes
                    or not partial and self.catalog.by_id(match.get("catalog_id")).get("status") != "matched"):
                return reward_preview_observation({}, self.catalog.skills, event, self.catalog,
                                                  {"method": "icon_cache_rejected", "fallback_reason": "cached_icon_not_confirmed"})
            self.counts["upstream_icon"] += 1
            identity = dict(match) if partial else self.catalog.by_id(match["catalog_id"])
            if identity.get("item_kind") == "skill_card" and not match.get("variant_verified"):
                # 历史本体匹配记录仍保留版本未知；新记录按具体模板确认版本。
                identity = {"status": "base_match_variant_unresolved", "database_name": match["database_name"],
                            "item_kind": "skill_card", "catalog_id": None, "effective_effect_dsl": None}
                if match.get("display_name"):
                    identity["display_name"] = match["display_name"]
            return reward_preview_observation({}, self.catalog.skills, event, self.catalog,
                                              {"method": event["observation_method"], "identity": identity, "identity_verified": True,
                                               "source_event_id": match.get("source_event_id"), "icon_path": match.get("icon_path"), "full_body_ocr": False})
        verified = event.get("detail_refreshed") is True and event.get("selected_highlight_verified") is True
        if not verified:
            return reward_preview_observation({}, self.catalog.skills, event, self.catalog, {"method": "unverified_detail"})
        digest = hashlib.sha256(image[487:778, 90:640].tobytes()).hexdigest()
        cache_key = (digest, bool(event.get("customization_hint")))
        if cache_key in self.details:
            self.counts["detail_cache"] += 1
            result = copy.deepcopy(self.details[cache_key])
            result.update(reward_page_id=event.get("reward_page_id"), candidate=event.get("candidate"))
            result["reading"] = {**result["reading"], "method": "detail_cache", "source_event_id": result["reading"]["source_event_id"]}
            self.details.move_to_end(cache_key)
        else:
            self.counts["name_ocr"] += 1
            tokens = {"name": read_roi(image, DETAIL_ROIS["name"])}
            result = reward_preview_observation(tokens, self.catalog.skills, event, self.catalog,
                                                {"method": "name_then_catalog", "full_body_ocr": False,
                                                 "source_event_id": event["event_id"]})
            self.details[cache_key] = copy.deepcopy(result)
            if len(self.details) > 128:
                self.details.popitem(last=False)
        return result


def reward_preview_observation(tokens_by_roi, catalog, event, rewards=None, reading=None):
    """将已核验的可见文字关联到候选图标，不推断实际获得的内容。"""
    refreshed = (event.get("observation_method") == "detail_ocr"
                 or event.get("detail_refreshed") is True and event.get("selected_highlight_verified") is True)
    names = sorted(tokens_by_roi.get("name", []), key=lambda token: (token["box"][1], token["box"][0]))
    reading = reading or {}
    cache_verified = reading.get("method") in {"icon_cache", "upstream_icon"} and reading.get("identity_verified") is True
    if rewards:
        identity = reading.get("identity") if cache_verified else rewards.match_tokens(names) if refreshed else rewards.match("")
        identity = dict(identity)
    else:
        identities = [catalog.match(token["text"], token.get("confidence", 0)) for token in names]
        matched = [identity for identity in identities if identity["status"] != "unknown"]
        unique_ids = {identity["database_card_id"] for identity in matched}
        identity = matched[0] if len(unique_ids) == 1 and refreshed else catalog.match("")
    effect_tokens = sorted(tokens_by_roi.get("effects", []), key=lambda token: (token["box"][1] // 14, token["box"][0]))
    rows = []
    for token in sorted(effect_tokens, key=lambda token: (token["box"][1], token["box"][0])):
        if rows and abs(rows[-1][0]["box"][1] - token["box"][1]) < 14:
            rows[-1].append(token)
        else:
            rows.append([token])
    effect_lines = [" ".join(token["text"] for token in sorted(row, key=lambda token: token["box"][0])) for row in rows]
    cost_tokens = tokens_by_roi.get("cost", [])
    deltas = [int(clean(token["text"]).replace("−", "-")) for token in cost_tokens
              if token.get("confidence", 0) >= .8 and re.fullmatch(r"-[0-9]{1,2}|0", clean(token["text"]).replace("−", "-"))]
    delta = deltas[0] if len(set(deltas)) == 1 and refreshed else None
    item_kind = identity.get("item_kind") or ("skill_card" if identity.get("database_card_id") else "other_or_unknown")
    resolved = catalog_effects(identity) if rewards else None
    effect_status = "unverified_detail"
    if refreshed or cache_verified:
        effect_status = ("catalog_default" if resolved and resolved["dsl"] and not reading.get("full_body_ocr")
                         else "ocr_text_only" if refreshed else "unverified_detail")
    if reading.get("fallback_reason") in ("customization_hint", "same_name_detail_changed"):
        identity["effective_effect_dsl"] = None
        identity["effect_resolution"] = "runtime_effect_needs_review"
        if resolved:
            resolved.update(source="catalog_reference_with_ocr", complete=False, current_effect_confirmed=False)
    return {
        "schema_version": 2,
        "reward_page_id": event.get("reward_page_id"),
        "candidate": event.get("candidate"),
        "item_kind": item_kind,
        "observed_name": identity.get("database_name") if cache_verified else " ".join(token["text"] for token in names) if refreshed else None,
        "identity": identity,
        "detail_verified": refreshed,
        "identity_verified": refreshed and bool(identity.get("catalog_id") or identity.get("database_card_id"))
                             or cache_verified and identity.get("status") == "matched",
        "art_identity_verified": cache_verified,
        "reading": {key: value for key, value in reading.items() if key != "identity"},
        "unknown_reason": "current_detail_not_opened" if cache_verified else None if refreshed else "detail_refresh_not_verified",
        "effects": {
            "visible_text": effect_lines if refreshed else [],
            "evidence": effect_tokens,
            "semantic_status": effect_status,
            "catalog": resolved,
            "complete": False,
        },
        "displayed_card_cost": {"value": delta, "unit": "header_signed_value", "evidence": cost_tokens, "actual_cost": None},
        "raw_ocr_by_roi": tokens_by_roi,
        "game_action_confirmed": None,
    }
