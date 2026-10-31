"""已核验名称的共享本体图标缓存与只读页面检索。"""

import os
import json
import math
import hashlib
from copy import deepcopy
from pathlib import Path
from contextlib import contextmanager

import numpy as np
from utils.image_io import bgr_image, encode_png, resize_image

# 保留完整图标，用框架绿幕遮罩排除边框、角标和强化符号。
CACHE_VERSION = 7
ICON_SIZE = 128
ITEM_KINDS = {"skill_card", "drink", "p_item"}


def icon_image(image, box, *, masked=True, item_kind="skill_card"):
    image = bgr_image(image)
    x, y, width, height = box
    if image.shape != (1280, 720, 3) or min(width, height) <= 8 or min(x, y) < 0 or x + width > 720 or y + height > 1280:
        raise ValueError("Unsupported reward icon bounds")
    normalized = resize_image(image[y:y + height, x:x + width], ICON_SIZE, ICON_SIZE)
    if masked:
        normalized = normalized.copy()
        normalized[~icon_mask(item_kind)] = (0, 255, 0)
    return bgr_image(normalized)


def icon_mask(item_kind="skill_card"):
    """饮料和 P 道具只屏蔽外框；卡牌额外屏蔽角标、加号和底部属性。"""
    if item_kind not in ITEM_KINDS:
        raise ValueError("Unsupported icon kind")
    y, x = np.ogrid[:ICON_SIZE, :ICON_SIZE]
    visible = (x >= 5) & (x < 123) & (y >= 5) & (y < 123)
    if item_kind != "skill_card":
        return visible
    visible &= y < 110
    visible &= ~((x < 52) & (y < 38))
    visible &= ~((x >= 88) & (y < 44))
    visible &= ~((x >= 94) & (y >= 44))
    visible &= ~((x < 44) & (y >= 82))
    visible &= ~((x >= 80) & (y >= 82))
    return visible


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
        self._scan_cache = None

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

    def _paths(self, entries):
        """只向框架提供缓存目录内通过完整性校验的文件。"""
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
        return paths

    def _score(self, icon, entries):
        """使用 MaaFramework 原生模板识别。"""
        from maa.pipeline import JTemplateMatch, JRecognitionType

        paths = self._paths(entries)
        if not paths or self.recognize is None:
            return 0.0
        results = self.recognize(JRecognitionType.TemplateMatch,
                                 JTemplateMatch(template=paths, threshold=[.96], method=5, green_mask=True), icon)
        return max((float(item.score) for item in results if math.isfinite(float(item.score))), default=0.0)

    def scan(self, image, operation):
        """检索外观一致的当前图标，排除明显变暗项；不据此确认可选或获得。"""
        from maa.pipeline import JTemplateMatch, JRecognitionType

        layouts = {
            "shop": ([50, 465, 620, 345], (128,), None),
            # 119/120 是同一布局的取整差异，统一按 120 检索，避免重复全区域扫描。
            "strengthen": ([65, 690, 600, 420], (120,), "skill_card"),
            "delete_copy_exchange": ([65, 390, 600, 720], (120,), "skill_card"),
            "move_cards": ([65, 390, 600, 720], (120, 128), "skill_card"),
        }
        data = self._load()
        if operation not in layouts or not data or self.recognize is None:
            return []
        image = bgr_image(image)
        if image.shape != (1280, 720, 3):
            return []
        (x, y, w, h), sizes, kind = layouts[operation]
        key = (operation, self._stamp, hashlib.sha256(image[y:y + h, x:x + w].tobytes()).digest())
        if self._scan_cache is not None and self._scan_cache[0] == key:
            return deepcopy(self._scan_cache[1])
        hits = []
        for size in sizes:
            scale = ICON_SIZE / size
            scene = resize_image(image[y:y + h, x:x + w], round(w * scale), round(h * scale))
            for entry in data["entries"]:
                if kind and entry["item_kind"] != kind:
                    continue
                paths = self._paths(entry)
                if not paths:
                    continue
                results = self.recognize(JRecognitionType.TemplateMatch,
                                         JTemplateMatch(template=paths, threshold=[.08], method=1, green_mask=True), scene)
                for result in results:
                    # 平方差越小越接近；all_results 仍需显式验收，不能沿用相关系数的高分规则。
                    difference = float(result.score)
                    if not math.isfinite(difference) or not 0 <= difference <= .08:
                        continue
                    left, top, _, _ = result.box
                    left, top = int(left), int(top)
                    crop = scene[top:top + ICON_SIZE, left:left + ICON_SIZE]
                    if crop.shape != (ICON_SIZE, ICON_SIZE, 3):
                        continue
                    # 再验证卡面结构，避免低对比度、颜色相近的不同卡通过平方差筛选。
                    verified = self.recognize(JRecognitionType.TemplateMatch,
                                             JTemplateMatch(template=paths, threshold=[.80], method=5, green_mask=True), crop)
                    score = max((float(item.score) for item in verified if math.isfinite(float(item.score))), default=0.)
                    if score < .80:
                        continue
                    # 匹配框就是完整图标，映射回原图；仅保留完整可见项。
                    bx, by = x + left / scale, y + top / scale
                    if bx < x - 3 or by < y - 3 or bx + size > x + w + 3 or by + size > y + h + 3:
                        continue
                    hits.append({"database_name": entry["database_name"], "item_kind": entry["item_kind"],
                                 "icon_path": paths[0], "box": [round(bx), round(by), size, size],
                                 "similarity": round(score, 4), "appearance_difference": round(difference, 4),
                                 "variant_verified": False})
        # 合并同位置的多尺度命中；同位置不同身份分数接近时保留未知。
        accepted = []
        while hits:
            best = max(hits, key=lambda hit: hit["similarity"])
            neighbors = [hit for hit in hits if abs(hit["box"][0] - best["box"][0]) < 20
                         and abs(hit["box"][1] - best["box"][1]) < 20]
            hits = [hit for hit in hits if hit not in neighbors]
            if best["similarity"] < .80:
                continue
            if any((hit["item_kind"], hit["database_name"]) != (best["item_kind"], best["database_name"])
                   and best["similarity"] - hit["similarity"] < .05 for hit in neighbors):
                continue
            accepted.append(best)
        accepted = sorted(accepted, key=lambda hit: (round(hit["box"][1] / 20), hit["box"][0]))
        self._scan_cache = (key, deepcopy(accepted))
        return accepted

    def lookup(self, image, box, item_kind=None):
        data = self._load()
        if not data:
            return None
        aligned_icon = icon_image(image, box, masked=False)
        x, y, width, height = box
        # 奖励裁框可能有少量像素偏差。保留原图标尺度，仅扩大搜索画布，
        # 不把扩展后的区域压回 128×128，也不搜索整页中的其他候选。
        left, top = max(0, x - 2), max(0, y - 2)
        right, bottom = min(720, x + width + 2), min(1280, y + height + 2)
        icon = resize_image(image[top:bottom, left:right],
                            round((right - left) * ICON_SIZE / width),
                            round((bottom - top) * ICON_SIZE / height))
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
                score = self._score(icon, entries)
                if width != ICON_SIZE or height != ICON_SIZE:
                    # 非标准尺寸扩大后取整会改变插值采样，保留原裁框作为额外候选。
                    score = max(score, self._score(aligned_icon, entries))
                families.append((score, entries))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError):
                continue
        families.sort(key=lambda item: item[0], reverse=True)
        if not families or families[0][0] < .98:
            return None
        score, entries = families[0]
        if any(score - other_score < .02 for other_score, _ in families[1:]):
            return None
        entry = entries[0]
        return {**entry, "variant_verified": False, "catalog_hashes": data["catalog_hashes"],
                "query_icon_sha256": hashlib.sha256(bgr_image(image[y:y + height, x:x + width]).tobytes()).hexdigest(),
                "similarity": round(score, 4),
                "method": "learned_icon_identity_only"}

    def learn(self, image, event, observation):
        if event.get("kind") == "route_before":
            # 仅确认按钮前的已选对象详情；不学习列表、强化后预览或未知布局。
            node = event.get("node")
            titles = {"ProduceChooseStrengthen": ("強化", "强化"),
                      "ProduceChooseDeleteClick": ("削除", "除去", "删除", "刪除", "移除", "コピー", "复制", "複製", "チェンジ", "变换", "變換")}
            if node not in titles or observation.get("error"):
                return None
            headers = [token for token in observation.get("raw_ocr", [])
                       if token.get("confidence", 0) >= .95 and 0 <= token["box"][0] < 190 and 20 <= token["box"][1] < 90]
            if not any(any(word in token["text"] for word in titles[node]) for token in headers):
                return None
            names = [c for c in observation.get("candidates", [])
                     if 185 <= c["name_box"][0] < 500 and 95 <= c["name_box"][1] < 150]
            if len(names) != 1:
                return None
            identity = names[0].get("identity") or {}
            if (identity.get("status") != "matched" or identity.get("match_method") != "exact"
                    or identity.get("ocr_confidence", 0) < .95 or not identity.get("database_card_id")
                    or identity.get("customization") or identity.get("observed_plus_count", 0) > 1):
                return None
            identity = {**identity, "item_kind": "skill_card", "catalog_id": identity["database_card_id"]}
            # 720×1280 确认页左上详情图标，与右侧名称绑定；不从点击坐标猜测列表对象。
            event = {**event, "candidate": {"box": [36, 109, 144, 144]}}
            observation = {"identity": identity, "selected_name_verified": True}
        elif event.get("kind") != "reward_candidate_preview":
            return None
        # 跨客户端进程序列化读改写；忙时跳过学习，不影响领取。
        self.directory.mkdir(parents=True, exist_ok=True)
        with cache_write_lock(self.directory / "write.lock") as acquired:
            if acquired:
                self._stamp = None
                return self._learn(image, event, observation)

    def _learn(self, image, event, observation):
        identity = observation.get("identity") or {}
        if (identity.get("item_kind") not in ITEM_KINDS or not (observation.get("detail_verified") or observation.get("selected_name_verified")) or identity.get("status") != "matched" or
                observation.get("reading", {}).get("fallback_reason") or event.get("customization_hint")):
            return
        icon = icon_image(image, event["candidate"]["box"], item_kind=identity["item_kind"])
        if float(icon[icon_mask(identity["item_kind"])].std()) < 1:
            return
        pixel_sha = hashlib.sha256(icon.tobytes()).hexdigest()
        data = self._load() or {"cache_version": CACHE_VERSION, "catalog_hashes": self.hashes, "scope": self.scope, "entries": []}
        previous = list(data["entries"])
        name = identity["database_name"].rstrip("+") if identity["item_kind"] == "skill_card" else identity["database_name"]
        same_identity = [entry for entry in previous if entry["item_kind"] == identity["item_kind"] and entry["database_name"] == name]
        for entry in same_identity:
            path = (self.directory / entry["icon_path"]).resolve()
            try:
                valid = path.parent in {self.directory / kind for kind in ITEM_KINDS} and hashlib.sha256(path.read_bytes()).hexdigest() == entry["icon_file_sha256"]
            except OSError:
                valid = False
            if valid:
                if len(same_identity) > 1:
                    data["entries"] = [item for item in previous if item not in same_identity or item is entry]
                    self._save(data, previous)
                return "reused"
        data["entries"] = [entry for entry in previous if entry not in same_identity]
        icon_path = self.directory / identity["item_kind"] / (hashlib.sha256(name.encode()).hexdigest()[:20] + "_" + pixel_sha[:12] + ".png")
        icon_path.parent.mkdir(parents=True, exist_ok=True)
        if not icon_path.exists():
            icon_path.write_bytes(encode_png(icon))
        entry = {"catalog_id": identity["catalog_id"], "database_name": name, "item_kind": identity["item_kind"],
                 "source_kind": event["kind"],
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
            if path.parent in {self.directory / kind for kind in ITEM_KINDS}:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


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
        root = Path(__file__).resolve().parents[2] / "debug/cache"
        return RewardIdentityCache(root, expected, scope, recognize)
    except (OSError, ValueError, KeyError, TypeError):
        return None
