"""奖励候选预览、身份缓存，以及优先读取名称的后台识别。"""

import re
import copy
import json
import time
import hashlib
from pathlib import Path
from collections import Counter, OrderedDict

import numpy as np
from utils.image_io import bgr_image, encode_png, decode_image, resize_image

from .catalog import RewardCatalog, clean, catalog_effects

# 第一版缓存使用 Pillow/RGB 重采样，不能与原生 BGR 缩略图混合比较。
CACHE_VERSION = 2


def icon_image(image, box):
    image = bgr_image(image)
    x, y, width, height = box
    if image.shape != (1280, 720, 3) or min(width, height) <= 8 or min(x, y) < 0 or x + width > 720 or y + height > 1280:
        raise ValueError("Unsupported reward icon bounds")
    # 保留角标和强化标记，只去掉图标自身的外框。
    crop = image[y + 4:y + height - 4, x + 4:x + width - 4]
    return resize_image(crop, 64, 64)


class RewardIdentityCache:
    def __init__(self, directory, hashes=None):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "reward_identity_cache.json"
        self.hashes = hashes
        self._stamp = None
        self._data = None
        self._icons = {}

    def _load(self):
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if self._data is not None and stamp == self._stamp:
                return self._data
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("cache_version") != CACHE_VERSION or len(data.get("entries", [])) > 128:
                return None
            if self.hashes is not None and data.get("catalog_hashes") != self.hashes:
                return None
            self._stamp, self._data = stamp, data
            return data
        except (OSError, ValueError, TypeError):
            return None

    def lookup(self, image, box):
        data = self._load()
        if not data:
            return None
        try:
            icon = np.asarray(icon_image(image, box)).astype(np.int16)
            matches = []
            for entry in data["entries"]:
                path = (self.directory / entry["icon_path"]).resolve()
                if not path.is_relative_to(self.directory):
                    continue
                key = entry["icon_file_sha256"]
                if key not in self._icons:
                    raw = path.read_bytes()
                    if hashlib.sha256(raw).hexdigest() != key:
                        continue
                    self._icons[key] = decode_image(raw).astype(np.int16)
                    if len(self._icons) > 128:
                        self._icons.pop(next(iter(self._icons)))
                before = self._icons[key]
                if before.shape != icon.shape:
                    continue
                delta = np.abs(before - icon)
                mean = float(delta.mean())
                fraction = float((delta.max(axis=2) > 24).mean())
                if mean <= 1.5 and fraction <= .01:
                    matches.append((mean, fraction, entry))
            # 已学习的身份互相冲突时，即使某项更接近也不复用。
            if len({entry["catalog_id"] for _, _, entry in matches}) != 1:
                return None
            mean, fraction, entry = min(matches, key=lambda value: value[0])
            return {**entry, "catalog_hashes": data["catalog_hashes"], "mean_delta": round(mean, 4),
                    "changed_fraction": round(fraction, 4), "method": "learned_icon_identity_only"}
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def learn(self, image, event, observation):
        identity = observation.get("identity") or {}
        if (not observation.get("detail_verified") or identity.get("status") != "matched" or
                identity.get("needs_full_ocr", True) or not identity.get("effective_effect_dsl") or
                observation.get("reading", {}).get("fallback_reason") or event.get("customization_hint")):
            return
        icon = icon_image(image, event["candidate"]["box"])
        pixel_sha = hashlib.sha256(icon.tobytes()).hexdigest()
        data = self._load() or {"cache_version": CACHE_VERSION, "catalog_hashes": self.hashes, "entries": []}
        if any(entry["icon_pixel_sha256"] == pixel_sha and entry["catalog_id"] == identity["catalog_id"] for entry in data["entries"]):
            return
        icon_path = self.directory / "derived/reward_icons" / (pixel_sha + ".png")
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        if not icon_path.exists():
            icon_path.write_bytes(encode_png(icon))
        entry = {"catalog_id": identity["catalog_id"], "database_name": identity["database_name"], "item_kind": identity["item_kind"],
                 "source_event_id": event["event_id"], "source_frame_sha256": event.get("frame", {}).get("sha256"),
                 "icon_path": icon_path.relative_to(self.directory).as_posix(), "icon_file_sha256": hashlib.sha256(icon_path.read_bytes()).hexdigest(),
                 "icon_pixel_sha256": pixel_sha}
        data["entries"] = [e for e in data["entries"] if not (e["icon_pixel_sha256"] == pixel_sha and e["catalog_id"] == entry["catalog_id"])]
        data["entries"].append(entry)
        data["entries"] = data["entries"][-128:]
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)


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
    pitch = min(gaps)
    if not w + 4 <= pitch <= w + 24 or any(abs(gap / pitch - round(gap / pitch)) > .04 for gap in gaps):
        return {"status": "ambiguous_spacing", "candidates": [], "selected_index": None}
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
    if not 2 <= len(boxes) <= 4 or any(not any(abs(seed[0] - box[0]) <= 3 for box in boxes) for seed in seeds):
        return {"status": "incomplete_frames", "candidates": [], "selected_index": None}
    scores = [selection_pixels(array, box) for box in boxes]
    selected = [i for i, score in enumerate(scores) if score >= 600]
    return {
        "status": "ready" if len(selected) == 1 else "selection_unconfirmed",
        "candidates": [{"candidate_id": f"reward_{i + 1}", "box": box, "highlight_pixels": scores[i]} for i, box in enumerate(boxes)],
        "selected_index": selected[0] if len(selected) == 1 else None,
        "detector": "single_row_frame_and_outer_orange_brackets_v1",
    }


def same_layout(left, right):
    a, b = left["candidates"], right["candidates"]
    return len(a) == len(b) and all(max(abs(x - y) for x, y in zip(c["box"], d["box"])) <= 3 for c, d in zip(a, b))


def verify_reward_layout(image, reference_image, reference_layout):
    """沿用已确认的图标位置核验页面，不重新拟合选中边框。"""
    array, before = rgb_image(image), rgb_image(reference_image)
    if array is None or before is None or array.shape != (1280, 720, 3) or before.shape != array.shape:
        return {"status": "unsupported_size", "candidates": [], "selected_index": None}
    candidates, differences = [], []
    for candidate in reference_layout["candidates"]:
        x, y, w, h = candidate["box"]
        original = before[y + 18:y + h - 18, x + 18:x + w - 18].astype(np.int16)
        current = array[y + 18:y + h - 18, x + 18:x + w - 18].astype(np.int16)
        delta = np.abs(original - current)
        differences.append({"candidate_id": candidate["candidate_id"], "mean_delta": round(float(delta.mean()), 4),
                            "changed_fraction": round(float((delta.max(axis=2) > 24).mean()), 4)})
        candidates.append({**candidate, "highlight_pixels": selection_pixels(array, candidate["box"])})
    consistent = bool(candidates) and all(d["mean_delta"] <= 3 and d["changed_fraction"] <= .03 for d in differences)
    selected = [i for i, candidate in enumerate(candidates) if candidate["highlight_pixels"] >= 600]
    return {
        "status": "candidate_art_changed" if not consistent else "ready" if len(selected) == 1 else "selection_unconfirmed",
        "candidates": candidates,
        "selected_index": selected[0] if consistent and len(selected) == 1 else None,
        "icon_differences": differences,
        "verification": "reference_icon_interiors_and_outer_brackets_v1",
    }


def detail_difference(left, right):
    a, b = rgb_image(left), rgb_image(right)
    return float(np.abs(a[487:778, 90:640].astype(np.int16) - b[487:778, 90:640].astype(np.int16)).mean())


class RewardPreview:
    """动作入口提供输入适配；OCR 在后台独立处理已保存的画面。"""

    def __init__(self, capture, screenshot, click, receive_visible, stopping, *, timeout=1.8, interval=.12):
        self.capture = capture
        self.screenshot = screenshot
        self.click = click
        self.receive_visible = receive_visible
        self.stopping = stopping
        self.timeout = timeout
        self.interval = interval
        self.original = None
        self.layout = None
        self.group_id = None
        self.changed_selection = False
        self.last_image = None
        self.last_wait_diagnostic = None
        self.identity_cache = RewardIdentityCache(capture.directory) if getattr(capture, "reward_identity_cache_enabled", False) else None

    def record(self, kind, image=None, **data):
        return self.capture.record(kind, image=image, reward_page_id=self.group_id, **data)

    def wait_selected(self, index, changed_from=None, expected=None, *, purpose="initial_selection", attempt=1):
        started = time.monotonic()
        deadline = started + self.timeout
        previous = None
        unchanged_stable = None
        first_failure = None
        counts = Counter()
        samples = 0
        last = {"reason": "no_observation", "observed_layout": None, "receive_button_visible": None}

        def finish(outcome, frame=None):
            diagnostic = {"purpose": purpose, "attempt": attempt, "candidate_id": f"reward_{index + 1}",
                          "outcome": outcome, "elapsed_seconds": round(time.monotonic() - started, 3),
                          "samples": samples, "reason_counts": dict(counts), **last}
            self.last_wait_diagnostic = diagnostic
            if outcome != "verified":
                if first_failure and first_failure[1]["sample"] != last.get("sample"):
                    self.record("reward_preview_wait_diagnostic", first_failure[0], phase="first_failure",
                                expected_layout=self.layout, **{**diagnostic, **first_failure[1]})
                self.record("reward_preview_wait_diagnostic", self.last_image, phase="last_observation",
                            expected_layout=self.layout, **diagnostic)
            return frame

        try:
            while time.monotonic() < deadline and not self.stopping():
                image = self.screenshot()
                self.last_image = image
                samples += 1
                layout = verify_reward_layout(image, self.original, self.layout) if self.original is not None else reward_layout(image)
                last = {"sample": samples, "frame_observed_at_ns": time.time_ns(),
                        "observed_layout": layout, "receive_button_visible": None,
                        "stable_delta": None, "refresh_delta": None, "restore_delta": None}
                if layout["status"] not in ("ready", "selection_unconfirmed"):
                    reason = "layout_" + layout["status"]
                elif not same_layout(self.layout, layout):
                    reason = "layout_mismatch"
                elif layout["selected_index"] is None:
                    reason = "highlight_unconfirmed"
                elif layout["selected_index"] != index:
                    reason = "different_candidate_selected"
                else:
                    last["receive_button_visible"] = bool(self.receive_visible(image))
                    if not last["receive_button_visible"]:
                        reason = "receive_button_missing"
                    elif previous is None:
                        reason = "awaiting_stable_detail"
                    else:
                        last["stable_delta"] = detail_difference(previous, image)
                        last["restore_delta"] = detail_difference(expected, image) if expected is not None else None
                        last["refresh_delta"] = detail_difference(changed_from, image) if changed_from is not None else None
                        if last["stable_delta"] >= .8:
                            reason = "detail_unstable"
                        elif expected is not None and last["restore_delta"] >= .8:
                            reason = "restore_detail_mismatch"
                        elif changed_from is not None and last["refresh_delta"] < .8:
                            reason = "detail_refresh_unconfirmed"
                            unchanged_stable = image
                        else:
                            last["reason"] = "verified"
                            return finish("verified", image)
                last["reason"] = reason
                counts[reason] += 1
                if first_failure is None:
                    first_failure = (image.copy(), dict(last))
                if layout["selected_index"] == index and last["receive_button_visible"]:
                    previous = image
                    if reason != "detail_refresh_unconfirmed":
                        unchanged_stable = None
                else:
                    previous = None
                    unchanged_stable = None
                time.sleep(self.interval)
        except Exception as exc:
            last.update(reason="observation_error", error=f"{type(exc).__name__}: {exc}"[:500])
            finish("observation_error")
            raise
        if self.stopping():
            last["reason"] = "task_stopping"
            return finish("task_stopping")
        return finish("stable_unchanged" if unchanged_stable is not None else "timeout", unchanged_stable)

    def select(self, index, purpose):
        box = self.layout["candidates"][index]["box"]
        point = [box[0] + box[2] // 2, box[1] + box[3] // 2]
        previous = self.last_image
        for attempt in range(1, 3 if purpose == "preview" else 2):
            if self.stopping():
                return None
            # 发送输入前先记录状态，异常可能发生在输入已经送出之后。
            self.changed_selection = True
            try:
                clicked = self.click(*point)
            except Exception as exc:
                self.last_wait_diagnostic = {"candidate_id": f"reward_{index + 1}", "purpose": purpose, "attempt": attempt,
                                             "outcome": "input_error", "reason": "input_error", "samples": 0,
                                             "error": f"{type(exc).__name__}: {exc}"[:500]}
                self.record("reward_preview_input", self.last_image, candidate_id=f"reward_{index + 1}", purpose=purpose,
                            attempt=attempt, actual_point=point, framework_action_succeeded=None,
                            error=f"{type(exc).__name__}: {exc}"[:500], observation_phase="last_known_before_input",
                            game_action_confirmed=None)
                raise
            self.record("reward_preview_input", candidate_id=f"reward_{index + 1}", purpose=purpose, attempt=attempt,
                        actual_point=point, framework_action_succeeded=bool(clicked), game_action_confirmed=None)
            if not clicked:
                self.last_wait_diagnostic = {"candidate_id": f"reward_{index + 1}", "purpose": purpose, "attempt": attempt,
                                             "outcome": "input_failed", "reason": "input_failed", "samples": 0}
                self.record("reward_preview_wait_diagnostic", self.last_image, candidate_id=f"reward_{index + 1}",
                            purpose=purpose, attempt=attempt, outcome="input_failed", reason="input_failed",
                            expected_layout=self.layout, actual_point=point, observation_phase="last_known_before_input")
                return None
            frame = self.wait_selected(index, changed_from=previous if purpose == "preview" else None,
                                       expected=self.original if purpose == "restore_policy_choice" else None,
                                       purpose=purpose, attempt=attempt)
            if frame is not None:
                return frame
            diagnostic = self.last_wait_diagnostic or {}
            layout = diagnostic.get("observed_layout") or {}
            # 只有候选行已核验且仍选中其他图标时才允许重试；
            # 目标已选中或页面已经变化时不重试。
            if (attempt != 1 or purpose != "preview" or self.stopping() or
                    diagnostic.get("reason") != "different_candidate_selected" or layout.get("status") != "ready" or
                    not self.receive_visible(self.last_image)):
                return None
            self.record("reward_preview_retry", self.last_image, candidate_id=f"reward_{index + 1}",
                        attempt=2, reason="target_not_selected_on_verified_row", observed_layout=layout)
        return None

    def collect(self, choice=None, confirmation_id=None):
        started = time.monotonic()
        image = self.screenshot()
        self.last_image = image
        self.layout = reward_layout(image)
        selected = self.layout["selected_index"]
        receive_visible = self.receive_visible(image)
        if self.layout["status"] != "ready" or not receive_visible:
            reason = self.layout["status"] if receive_visible else "receive_button_missing"
            self.record("reward_preview_skipped", reason=reason, image=image)
            return {"safe_to_receive": receive_visible and not self.stopping(), "status": "skipped", "reason": reason}
        image = self.wait_selected(selected)
        if image is None:
            self.record("reward_preview_skipped", reason="initial_selection_unstable")
            return {"safe_to_receive": False, "status": "skipped", "reason": "initial_selection_unstable"}
        self.original = image.copy()
        self.last_image = self.original
        self.group_id = self.capture.record(
            "reward_page_start", image=image, layout=self.layout, selected_candidate_id=f"reward_{selected + 1}",
            policy_choice=choice, decision_id=(choice or {}).get("decision_id"), confirmation_id=confirmation_id,
            detail_rois=DETAIL_ROIS, candidates_complete=False,
        )
        if not self.group_id:
            return {"safe_to_receive": True, "status": "skipped", "reason": "capture_unavailable"}
        recorded = []
        reason = None
        failure_diagnostic = None
        safe = True
        try:
            for index in [selected, *[i for i in range(len(self.layout["candidates"])) if i != selected]]:
                if self.stopping():
                    reason = "task_stopping"
                    break
                cached = (self.identity_cache.lookup(self.original, self.layout["candidates"][index]["box"])
                          if self.identity_cache and index != selected else None)
                if cached:
                    event_id = self.record("reward_candidate_preview", self.original, candidate=self.layout["candidates"][index],
                                           observation_method="icon_cache", cached_identity=cached, detail_refreshed=False,
                                           selected_highlight_verified=False, game_action_confirmed=None)
                    if not event_id:
                        reason = "capture_unavailable"
                        break
                    recorded.append(event_id)
                    continue
                previous = self.last_image
                frame = self.original if index == selected else self.select(index, "preview")
                if frame is None:
                    reason = "preview_not_verified"
                    failure_diagnostic = self.last_wait_diagnostic
                    break
                refreshed = index == selected or detail_difference(previous, frame) >= .8
                event_id = self.record(
                    "reward_candidate_preview", frame, candidate=self.layout["candidates"][index],
                    detail_rois=DETAIL_ROIS, detail_refreshed=refreshed,
                    selected_highlight_verified=True, game_action_confirmed=None,
                    wait_diagnostic=self.last_wait_diagnostic,
                )
                if not event_id:
                    reason = "capture_unavailable"
                    break
                recorded.append(event_id)
        except Exception as exc:
            reason = f"preview_error:{type(exc).__name__}"
            failure_diagnostic = self.last_wait_diagnostic
        finally:
            if self.changed_selection:
                safe = False
                if not self.stopping():
                    try:
                        frame = self.select(selected, "restore_policy_choice")
                        safe = frame is not None and detail_difference(self.original, frame) < .8
                    except Exception:
                        safe = False
                self.record("reward_selection_restored", self.last_image,
                            selected_candidate_id=f"reward_{selected + 1}", verified=safe, game_action_confirmed=None)
            else:
                self.record("reward_selection_restored", self.original, selected_candidate_id=f"reward_{selected + 1}",
                            verified=not self.stopping(), method="original_selection_unchanged", game_action_confirmed=None)
            safe = safe and not self.stopping()
            self.record("reward_page_end", previews=recorded, expected_count=len(self.layout["candidates"]),
                        candidate_previews_complete=len(recorded) == len(self.layout["candidates"]) and reason is None,
                        safe_to_receive=safe, stop_reason=reason, failure_diagnostic=failure_diagnostic,
                        elapsed_seconds=round(time.monotonic() - started, 3))
        return {"safe_to_receive": safe, "status": "collected", "count": len(recorded), "reason": reason}


class RewardReader:
    def __init__(self, skills, items_path, directory=None):
        self.catalog = RewardCatalog(skills, items_path)
        self.icons = RewardIdentityCache(directory, self.catalog.hashes) if directory else None
        self.details = OrderedDict()
        self.seen_names = {}
        self.counts = {"name_ocr": 0, "body_ocr": 0, "cost_ocr": 0, "detail_cache": 0, "icon_cache": 0}

    def read(self, image, event, read_roi):
        cached_icon = event.get("cached_identity")
        if event.get("observation_method") == "icon_cache":
            match = self.icons.lookup(image, event["candidate"]["box"]) if self.icons else None
            if not match or not cached_icon or match["catalog_id"] != cached_icon.get("catalog_id"):
                return reward_preview_observation({}, self.catalog.skills, event, self.catalog,
                                                  {"method": "icon_cache_rejected", "fallback_reason": "cached_icon_not_confirmed"})
            self.counts["icon_cache"] += 1
            identity = self.catalog.by_id(match["catalog_id"])
            return reward_preview_observation({}, self.catalog.skills, event, self.catalog,
                                              {"method": "icon_cache", "identity": identity, "identity_verified": True,
                                               "source_event_id": match["source_event_id"], "full_body_ocr": False})
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
            identity = self.catalog.match_tokens(tokens["name"])
            identifier = identity.get("catalog_id")
            reason = ("customization_hint" if event.get("customization_hint") else "unknown_or_missing_catalog_effect"
                      if identity.get("needs_full_ocr", True) else "same_name_detail_changed"
                      if identifier in self.seen_names and digest not in self.seen_names[identifier] else None)
            if reason:
                self.counts["body_ocr"] += 1
                self.counts["cost_ocr"] += 1
                tokens.update(effects=read_roi(image, DETAIL_ROIS["effects"]), cost=read_roi(image, DETAIL_ROIS["cost"]))
            result = reward_preview_observation(tokens, self.catalog.skills, event, self.catalog,
                                                {"method": "name_then_catalog" if not reason else "full_ocr_fallback",
                                                 "fallback_reason": reason, "full_body_ocr": bool(reason), "source_event_id": event["event_id"]})
            if identifier:
                self.seen_names.setdefault(identifier, set()).add(digest)
                if len(self.seen_names[identifier]) > 4:
                    self.seen_names[identifier] = {digest}
                if len(self.seen_names) > 128:
                    self.seen_names.pop(next(iter(self.seen_names)))
            self.details[cache_key] = copy.deepcopy(result)
            if len(self.details) > 128:
                self.details.popitem(last=False)
        if self.icons:
            try:
                self.icons.learn(image, event, result)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result["reading"]["cache_error"] = str(exc)[:250]
        return result


def reward_preview_observation(tokens_by_roi, catalog, event, rewards=None, reading=None):
    """将已核验的可见文字关联到候选图标，不推断实际获得的内容。"""
    refreshed = event.get("detail_refreshed") is True and event.get("selected_highlight_verified") is True
    names = sorted(tokens_by_roi.get("name", []), key=lambda token: (token["box"][1], token["box"][0]))
    reading = reading or {}
    cache_verified = reading.get("method") == "icon_cache" and reading.get("identity_verified") is True
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
        "identity_verified": refreshed and bool(identity.get("catalog_id") or identity.get("database_card_id")) or cache_verified,
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
