"""奖励候选预览、身份缓存，以及优先读取名称的同步识别。"""

import os
import re
import copy
import json
import time
import hashlib
from pathlib import Path
from contextlib import contextmanager
from collections import Counter, OrderedDict

import numpy as np
from utils.image_io import bgr_image, encode_png

from .catalog import RewardCatalog, clean, catalog_effects

# 原尺寸裁切模板，不复用旧版 64×64 缩略图。
CACHE_VERSION = 4


def icon_image(image, box):
    image = bgr_image(image)
    x, y, width, height = box
    if image.shape != (1280, 720, 3) or min(width, height) <= 8 or min(x, y) < 0 or x + width > 720 or y + height > 1280:
        raise ValueError("Unsupported reward icon bounds")
    # 保留角标和强化标记，只去掉图标自身的外框。
    crop = image[y + 4:y + height - 4, x + 4:x + width - 4]
    return bgr_image(crop)


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


class RewardIdentityCache:
    def __init__(self, directory, hashes=None, scope=None, recognize=None):
        self.directory = Path(directory).resolve()
        self.path = self.directory / "reward_identity_cache.json"
        self.hashes = hashes
        self.scope = scope
        self.recognize = recognize
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
            if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
                return None
            if data.get("cache_version") != CACHE_VERSION or len(data.get("entries", [])) > 128:
                return None
            if self.hashes is not None and data.get("catalog_hashes") != self.hashes:
                return None
            if data.get("scope") != self.scope:
                return None
            self._stamp, self._data = stamp, data
            return data
        except (OSError, ValueError, TypeError):
            return None

    def _score(self, icon, entries):
        """使用 MaaFramework 原生模板识别；不另实现图像匹配算法。"""
        from maa.pipeline import JTemplateMatch, JRecognitionType

        if self.recognize is None:
            return 0.0
        entries = entries if isinstance(entries, list) else [entries]
        paths = []
        for entry in entries:
            path = (self.directory / entry["icon_path"]).resolve()
            if not path.is_relative_to(self.directory):
                continue
            key = entry["icon_file_sha256"]
            try:
                if key not in self._icons:
                    if hashlib.sha256(path.read_bytes()).hexdigest() != key:
                        continue
                    self._icons[key] = True
                    if len(self._icons) > 128:
                        self._icons.pop(next(iter(self._icons)))
                paths.append(path.as_posix())
            except OSError:
                continue
        if not paths:
            return 0.0
        results = self.recognize(JRecognitionType.TemplateMatch,
                                 JTemplateMatch(template=paths, threshold=[.96], method=5), icon)
        return max((float(item.score) for item in results), default=0.0)

    def lookup(self, image, box, item_kind=None):
        data = self._load()
        if not data:
            return None
        icon_image(image, box)  # 核验原图尺寸与候选区域边界。
        x, y, width, height = box
        icon = bgr_image(image[y:y + height, x:x + width])
        groups = {}
        for entry in data["entries"]:
            if item_kind and entry["item_kind"] != item_kind:
                continue
            family = (entry["item_kind"], entry["database_name"].rstrip("+")
                      if entry["item_kind"] == "skill_card" else entry["catalog_id"])
            groups.setdefault(family, []).append(entry)
        families = []
        for entries in groups.values():
            try:
                families.append((self._score(icon, entries), entries))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                continue
        families.sort(key=lambda item: item[0], reverse=True)
        if not families or families[0][0] < .98:
            return None
        score, entries = families[0]
        if any(score - other_score < .02 for other_score, _ in families[1:]):
            return None
        if len(entries) == 1:
            matches = [(score, entries[0])]
        else:
            # 同卡家族先合并调用，只有命中家族才逐变体核验强化标记。
            matches = [(self._score(icon, entry), entry) for entry in entries]
        matches.sort(key=lambda item: item[0], reverse=True)
        if not matches or matches[0][0] < .98:
            return None
        score, entry = matches[0]
        # 相近身份（包括不同强化版本）不得仅因排名靠前就直接复用。
        if any(other["catalog_id"] != entry["catalog_id"] and score - value < .02 for value, other in matches[1:]):
            return None
        return {**entry, "catalog_hashes": data["catalog_hashes"], "query_icon_sha256": hashlib.sha256(icon.tobytes()).hexdigest(),
                "similarity": round(score, 4),
                "method": "learned_icon_identity_only"}

    def learn(self, image, event, observation):
        # 跨客户端进程序列化读改写；忙时跳过学习，不影响领取。
        self.directory.mkdir(parents=True, exist_ok=True)
        with cache_write_lock(self.directory / "write.lock") as acquired:
            if acquired:
                self._stamp = None
                return self._learn(image, event, observation)

    def _learn(self, image, event, observation):
        identity = observation.get("identity") or {}
        if (not observation.get("detail_verified") or identity.get("status") != "matched" or
                observation.get("reading", {}).get("fallback_reason") or event.get("customization_hint")):
            return
        icon = icon_image(image, event["candidate"]["box"])
        if float(icon.std()) < 1:
            return
        pixel_sha = hashlib.sha256(icon.tobytes()).hexdigest()
        data = self._load() or {"cache_version": CACHE_VERSION, "catalog_hashes": self.hashes, "scope": self.scope, "entries": []}
        previous = list(data["entries"])
        same_identity = [entry for entry in previous if entry["catalog_id"] == identity["catalog_id"]]
        for entry in same_identity:
            path = (self.directory / entry["icon_path"]).resolve()
            try:
                valid = path.is_relative_to(self.directory / "icons") and hashlib.sha256(path.read_bytes()).hexdigest() == entry["icon_file_sha256"]
            except OSError:
                valid = False
            if valid:
                if len(same_identity) > 1:
                    data["entries"] = [item for item in previous if item not in same_identity or item is entry]
                    self._save(data, previous)
                return "reused"
        data["entries"] = [entry for entry in previous if entry not in same_identity]
        icon_path = self.directory / "icons" / (re.sub(r"[^A-Za-z0-9_-]", "_", identity["catalog_id"]) + "_" + pixel_sha[:12] + ".png")
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
        self._save(data, previous)
        return "stored"

    def _save(self, data, previous):
        """先更新索引，再移除已合并或超出容量的已知图标文件；由调用方持有写锁。"""
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)
        self._stamp = None
        retained = {entry["icon_path"] for entry in data["entries"]}
        for entry in previous:
            if entry["icon_path"] in retained:
                continue
            path = (self.directory / entry["icon_path"]).resolve()
            if path.is_relative_to(self.directory / "icons"):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


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
    """动作入口提供输入适配；OCR 直接处理当前内存画面。"""

    def __init__(self, capture, screenshot, click, receive_visible, stopping, *, recognize=None, context=None, item_kind=None, timeout=1.8, interval=.12):
        self.capture = capture
        self.context = context
        self.item_kind = item_kind
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
        self.preview_clicks = 0
        self.identity_cache = cache_for_capture(capture.directory, recognize=recognize) if getattr(capture, "reward_identity_cache_enabled", False) else None

    def record(self, kind, image=None, **data):
        return self.capture.record(kind, image=image, context=self.context, reward_page_id=self.group_id, reward_kind=self.item_kind, **data)

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
                self.preview_clicks += 1
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
            self.record("reward_preview_skipped", reason=reason, image=image, observed_layout=self.layout)
            return {"safe_to_receive": receive_visible and not self.stopping(), "status": "skipped", "reason": reason}
        image = self.wait_selected(selected)
        if image is None:
            self.record("reward_preview_skipped", reason="initial_selection_unstable")
            return {"safe_to_receive": False, "status": "skipped", "reason": "initial_selection_unstable"}
        self.original = image.copy()
        self.last_image = self.original
        self.group_id = self.capture.record(
            "reward_page_start", image=image, context=self.context, layout=self.layout, selected_candidate_id=f"reward_{selected + 1}",
            policy_choice=choice, decision_id=(choice or {}).get("decision_id"), confirmation_id=confirmation_id,
            detail_rois=DETAIL_ROIS, candidates_complete=False,
        )
        if not self.group_id:
            return {"safe_to_receive": True, "status": "skipped", "reason": "capture_unavailable"}
        recorded = []
        cache_hits = 0
        reason = None
        failure_diagnostic = None
        safe = True
        try:
            for index in [selected, *[i for i in range(len(self.layout["candidates"])) if i != selected]]:
                if self.stopping():
                    reason = "task_stopping"
                    break
                cached = (self.identity_cache.lookup(self.original, self.layout["candidates"][index]["box"], self.item_kind)
                          if self.identity_cache else None)
                if cached:
                    cache_hits += 1
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
            self.record("reward_page_end", previews=recorded, cache_hits=cache_hits, preview_clicks=self.preview_clicks, expected_count=len(self.layout["candidates"]),
                        candidate_previews_complete=len(recorded) == len(self.layout["candidates"]) and reason is None,
                        safe_to_receive=safe, stop_reason=reason, failure_diagnostic=failure_diagnostic,
                        elapsed_seconds=round(time.monotonic() - started, 3))
        return {"safe_to_receive": safe, "status": "collected", "count": len(recorded), "reason": reason}


class RewardReader:
    def __init__(self, skills, items_path, directory=None, recognize=None):
        self.catalog = RewardCatalog(skills, items_path)
        self.icons = cache_for_capture(directory, self.catalog.hashes, recognize) if directory else None
        self.details = OrderedDict()
        self.counts = {"name_ocr": 0, "detail_cache": 0, "icon_cache": 0}

    def read(self, image, event, read_roi):
        cached_icon = event.get("cached_identity")
        if event.get("observation_method") == "icon_cache":
            x, y, width, height = event["candidate"]["box"]
            digest = hashlib.sha256(bgr_image(image[y:y + height, x:x + width]).tobytes()).hexdigest()
            match = cached_icon
            if (not match or match.get("query_icon_sha256") != digest
                    or match.get("catalog_hashes") != self.catalog.hashes
                    or self.catalog.by_id(match.get("catalog_id")).get("status") != "matched"):
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
            result = reward_preview_observation(tokens, self.catalog.skills, event, self.catalog,
                                                {"method": "name_then_catalog", "full_body_ocr": False,
                                                 "source_event_id": event["event_id"]})
            self.details[cache_key] = copy.deepcopy(result)
            if len(self.details) > 128:
                self.details.popitem(last=False)
        if self.icons:
            try:
                result["reading"]["cache_write"] = self.icons.learn(image, event, result)
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


@contextmanager
def cache_write_lock(path):
    """使用系统文件锁，进程退出自动释放，避免遗留锁阻断后续学习。"""
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        try:
            yield acquired
        finally:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def cache_for_capture(directory, hashes=None, recognize=None):
    """统一缓存目录；格式、卡库或识别布局变化时重新学习，不迁移旧测试缓存。"""
    try:
        manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("reward_identity_cache_enabled") is False:
            return None
        scope = manifest["metadata"]["reward_cache"]
        expected = scope["catalog_hashes"]
        if hashes is not None and hashes != expected:
            return None
        root = Path(__file__).resolve().parents[2] / "debug/cache/reward_identity"
        return RewardIdentityCache(root, expected, scope, recognize)
    except (OSError, ValueError, KeyError, TypeError):
        return None
