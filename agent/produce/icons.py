"""只读上游图标匹配；所有缩放、遮罩与特征仅驻留内存，不生成模板文件。"""

import json
import math
import hashlib
from pathlib import Path
from functools import lru_cache

import numpy as np
from utils.image_io import bgr_image, read_image, resize_image

from .catalog import translated_name
from .resources import ICON_FOLDERS
from .state import PRODUCE_FRAME_SIZE, scale_box

ICON_SIZE = 128
ITEM_KINDS = {"skill_card", "drink", "p_item"}
MATCH_THRESHOLD = .70
MATCH_MARGIN = .05
GRAY_BRIGHTNESS = 120
# 上游 gakumas-data idols.json 的稳定 ID；未知偶像保留全部变体。
IDOL_NAMES = ("花海咲季", "月村手毬", "藤田ことね", "有村麻央", "葛城リーリヤ", "倉本千奈",
              "紫雲清夏", "篠澤広", "姫崎莉波", "花海佑芽", "十王星南", "秦谷美鈴", "雨夜燕")


def icon_operation_available(status, operation):
    """图库不完整时，图标专用环节沿用精简模式操作；其他识别继续。"""
    if status is None:
        return True
    if operation in {"shop", "shop_confirm", "shop_unavailable", "shop_exit"}:
        return all(status.get(kind, {}).get("complete", False) for kind in ICON_FOLDERS)
    if operation in {"strengthen", "strengthen_confirm", "delete_copy_exchange", "delete_copy_exchange_confirm", "move_cards"}:
        return status.get("skill_card", {}).get("complete", False)
    return True


def drink_scroll(image):
    """从右边轨道和滑块读出列表可视区和已滚过的像素数。"""
    rail = image[235:1090, 675:680].mean(axis=2).min(axis=1)
    edges = np.flatnonzero(np.diff(rail < 238, prepend=False, append=False))
    for start, end in edges.reshape(-1, 2):
        if end - start < 300:
            continue
        thumb = np.flatnonzero(rail[start:end] < 178)
        if len(thumb) < 40:
            continue
        travel = end - start - (thumb[-1] - thumb[0] + 1)
        offset = int(round(thumb[0] * (end - start) / (thumb[-1] - thumb[0] + 1)))
        return int(235 + start), int(235 + end), offset, thumb[0] <= 4, travel - thumb[0] <= 4
    return 235, 1090, 0, True, True


def gray(image):
    return np.rint(image[:, :, 0] * .114 + image[:, :, 1] * .587 + image[:, :, 2] * .299).astype(np.float32)


def normalized(values):
    values = values.astype(np.float32)
    values -= values.mean(axis=-1, keepdims=True)
    return values / np.maximum(np.linalg.norm(values, axis=-1, keepdims=True), 1e-9)


def icon_mask(item_kind="skill_card"):
    """仅屏蔽外框，保留角标、费用和加号以区分普通版与强化版。"""
    if item_kind not in ITEM_KINDS:
        raise ValueError("Unsupported icon kind")
    y, x = np.ogrid[:ICON_SIZE, :ICON_SIZE]
    visible = (x >= 5) & (x < 123) & (y >= 5) & (y < 123)
    return visible


def identity_mask(item_kind="skill_card"):
    """技能卡用中央插画认卡名，边缘的费用、效果标记和加号留给版本核验。"""
    if item_kind == "skill_card":
        y, x = np.ogrid[:ICON_SIZE, :ICON_SIZE]
        return (x >= 40) & (x < 92) & (y >= 38) & (y < 90)
    return icon_mask(item_kind)


def feature(image, kind):
    small_mask = identity_mask(kind)[::4, ::4]
    small = gray(resize_image(image, 32, 32))
    return normalized(small[small_mask])


@lru_cache(maxsize=2)
def load_gallery(data_directory, idol_id):
    """每个资源目录/偶像只解码一次；缺图跳过，安装更新后显式清除缓存。"""
    directory = Path(data_directory)
    cards = json.loads((directory / "produce_skill_cards.json").read_text(encoding="utf-8"))["cards"]
    items = json.loads((directory / "produce_reward_items.json").read_text(encoding="utf-8"))
    gallery = {}
    for kind, folder, rows in (("skill_card", "skillCards", cards), ("drink", "pDrinks", items["drinks"]),
                               ("p_item", "pItems", items["p_items"])):
        by_id = {str(row["id"]): row for row in rows}
        entries, vectors, pixels, originals = [], [], [], []
        for path in sorted((directory / "images" / folder).glob("*.png")):
            parts = path.stem.split("_")
            row = by_id.get(parts[0])
            if row is None or (kind == "skill_card" and idol_id and len(parts) > 1 and parts[-1] != str(idol_id)):
                continue
            try:
                image = resize_image(read_image(path), ICON_SIZE, ICON_SIZE)
            except (OSError, ValueError, RuntimeError):
                continue
            entries.append({"catalog_id": f"{kind}_{row['id']:04d}", "database_name": row["name"],
                            "name_zh_cn": translated_name(row), "item_kind": kind, "icon_path": path.as_posix()})
            originals.append(gray(image).astype(np.uint8))
            vectors.append(feature(image, kind))
            pixels.append(normalized(gray(image)[identity_mask(kind)]))
        if entries:
            gallery[kind] = (entries, np.stack(vectors), np.stack(pixels), np.stack(originals))
    return gallery


class UpstreamIcons:
    def __init__(self, directory, idol_id=None, hashes=None, *, icon_resources=None, localized_names=False):
        self.directory = Path(directory).resolve()
        self.idol_id = idol_id
        self.hashes = hashes
        self.icon_resources = icon_resources
        self.localized_names = localized_names
        self.available_kinds = {kind for kind in ICON_FOLDERS
                                if icon_resources is None or icon_resources.get(kind, {}).get("complete", False)}
        self._last_scan = None

    def lookup(self, image, box, item_kind=None, *, allow_unverified_variant=False):
        """全流派初筛，每个身份至多两图精匹配，分数和分差均通过才确认具体版本。"""
        if (not self.available_kinds or item_kind and item_kind not in self.available_kinds
                or item_kind is None and len(self.available_kinds) < len(ICON_FOLDERS)):
            return None
        image = bgr_image(image)
        x, y, width, height = map(int, box)
        if image.shape != (1280, 720, 3) or min(x, y) < 2 or min(width, height) < 16 or x + width + 2 > 720 or y + height + 2 > 1280:
            return None
        crop = image[y:y + height, x:x + width]
        if gray(crop).mean() < GRAY_BRIGHTNESS:
            return None
        query = resize_image(image[y-2:y+height+2, x-2:x+width+2], 132, 132)
        candidates = []
        for kind, (entries, vectors, pixels, originals) in load_gallery(str(self.directory), self.idol_id).items():
            if kind not in self.available_kinds or item_kind and kind != item_kind:
                continue
            rank = np.argsort(np.einsum("ij,j->i", vectors, feature(query[2:130, 2:130], kind)))[::-1]
            families = {}
            for index in rank:
                name = entries[index]["database_name"].rstrip("+") if kind == "skill_card" else entries[index]["database_name"]
                if name not in families and len(families) < 8:
                    families[name] = []
                if name in families and len(families[name]) < 2:
                    families[name].append(index)
            mono = gray(query)
            windows = np.stack([normalized(mono[dy:dy+128, dx:dx+128][identity_mask(kind)]) for dy in range(5) for dx in range(5)])
            for indices in families.values():
                scores = np.einsum("ik,jk->ij", pixels[indices], windows).max(axis=1)
                best = int(np.argmax(scores))
                entry = dict(entries[indices[best]])
                candidates.append((float(scores[best]), entry, kind, entries, originals, mono))
        candidates.sort(key=lambda value: value[0], reverse=True)
        if not candidates or candidates[0][0] < MATCH_THRESHOLD:
            return None
        score, entry, kind, entries, originals, mono = candidates[0]
        if len(candidates) > 1 and score - candidates[1][0] < MATCH_MARGIN:
            return None
        entry["variant_verified"] = True
        if kind == "skill_card":
            # 初筛按卡名聚合，版本核验必须包含同卡的全部版本，不能让候选截断隐藏竞争者。
            base = entry["database_name"].rstrip("+")
            variants = [i for i, candidate in enumerate(entries) if candidate["database_name"].rstrip("+") == base]
            names = {entries[i]["database_name"] for i in variants}
            if len(names) > 1:
                art = originals[variants].astype(np.float32)
                difference = (art.max(axis=0) - art.min(axis=0) > 20) & icon_mask(kind)
                entry["variant_verified"] = False
                if difference.sum() >= 16:
                    expanded = gray(resize_image(np.pad(image, ((8, 8), (8, 8), (0, 0)), mode="edge")[y:y+height+16, x:x+width+16],144,144))
                    # 游戏和上游图标的数字、费用与加号并非整体平移，分别核验局部差异。
                    region_scores = []
                    for rx, ry, rw, rh in ((5,5,50,36),(86,5,37,39),(92,40,31,43),(5,82,45,41),(78,82,45,41)):
                        if difference[ry:ry+rh,rx:rx+rw].sum() < 16:
                            continue
                        local_mask = difference[ry:ry+rh,rx:rx+rw]
                        references = normalized(art[:,ry:ry+rh,rx:rx+rw][:,local_mask])
                        windows = np.stack([normalized(expanded[ry+dy:ry+dy+rh,rx+dx:rx+dx+rw][local_mask]) for dy in range(17) for dx in range(17)])
                        region_scores.append(np.einsum("ik,jk->ij",references,windows).max(axis=1))
                    local_scores = np.mean(region_scores,axis=0) if region_scores else np.zeros(len(variants))
                    by_name = {}
                    for i, local_score in zip(variants, local_scores):
                        name = entries[i]["database_name"]
                        if name not in by_name or local_score > by_name[name][0]:
                            by_name[name] = (float(local_score), i)
                    ordered = sorted(by_name.values(), reverse=True)
                    if ordered[0][0] >= MATCH_THRESHOLD and ordered[0][0] - ordered[1][0] >= MATCH_MARGIN:
                        entry = {**entries[ordered[0][1]], "variant_verified": True,
                                 "variant_similarity": round(ordered[0][0], 4)}
        if not entry["variant_verified"]:
            if not allow_unverified_variant:
                return None
            # 名称分数与分差已经通过；只保留卡名，不把初筛版本当成已确认的库条目。
            translated_bases = {entries[i]["name_zh_cn"].rstrip("+＋") for i in variants if entries[i]["name_zh_cn"]}
            entry = {"database_name": base, "base_name": base, "item_kind": kind,
                     "name_zh_cn": translated_bases.pop() if len(translated_bases) == 1 else None,
                     "catalog_id": None, "icon_path": None, "variant_verified": False,
                     "status": "base_match_variant_unresolved", "upgrade_status": "unresolved", "upgraded": None}
        name_zh_cn = entry.pop("name_zh_cn", None)
        if self.localized_names and name_zh_cn:
            entry["display_name"] = name_zh_cn
        return {**entry, "similarity": round(score, 4), "catalog_hashes": self.hashes,
                "query_icon_sha256": hashlib.sha256(crop.tobytes()).hexdigest(), "method": "upstream_icon_variant"}

    def drink_rows(self, image, top, bottom):
        """按右侧勾选框定位完整行；同名瓶保留各自的位置。"""
        band = image[top:bottom, 615:640].astype(np.int16)
        blue, green, red = band.transpose(2, 0, 1)
        orange = (red - blue > 100) & (red - green > 45)
        grey = (abs(red - blue) < 18) & (green > 150) & (green < 228)
        mask = (orange | grey).sum(axis=1) >= 10
        edges = np.flatnonzero(np.diff(mask, prepend=False, append=False))
        rows = []
        for start, end in edges.reshape(-1, 2):
            y = int(top + (start + end) // 2)
            if y - 42 < top or y + 42 > bottom:
                continue
            if not 18 <= end - start <= 32:
                return []
            match = self.lookup(image, [65, y - 36, 72, 72], "drink")
            if not match:
                return []
            rows.append((y, match["catalog_id"], bool(orange[start:end].mean() > .35)))
        return rows

    def scan(self, image, operation, *, allow_unverified_variant=False):
        """仅观察已显示的完整图标，不增加输入，也不用 OCR 补牌名。"""
        regions = {"strengthen": (65, 690, 600, 420), "delete_copy_exchange": (65, 390, 600, 720),
                   "move_cards": (65, 390, 600, 720), "shop": (50, 465, 620, 345)}
        if operation not in regions or not icon_operation_available(self.icon_resources, operation):
            return []
        image = bgr_image(image)
        if image.shape != (1280, 720, 3):
            return []
        key = (operation, hashlib.sha256(image.tobytes()).digest())
        if not self._last_scan or self._last_scan[0] != key:
            hits = []
            for box in locate_icons(image, regions[operation], (128,) if operation == "shop" else (120, 128), operation):
                match = self.lookup(image, box, None if operation == "shop" else "skill_card", allow_unverified_variant=True)
                if match:
                    hits.append({**match, "box": box})
            self._last_scan = (key, hits)
        return [dict(hit) for hit in self._last_scan[1] if allow_unverified_variant or hit["variant_verified"]]


class BattleDrinkReader:
    """定位战斗圆形瓶位，用圆框内的彩色瓶身与本次会话的饮料图库核对。"""

    def __init__(self, recognize, directory, catalog, icon_resources):
        self.recognize = recognize
        self.directory = directory
        self.catalog = catalog
        self.ids = []
        self.vectors = None
        self.gallery_pending = icon_resources is None or icon_resources.get("drink", {}).get("complete", False)
        y, x = np.ogrid[:32, :32]
        self.mask = (x - 15.5) ** 2 + (y - 15.5) ** 2 < 14 ** 2

    def _feature(self, image):
        return normalized(resize_image(image, 32, 32)[self.mask].ravel())

    def _load_gallery(self):
        self.gallery_pending = False
        vectors = []
        ids = []
        for catalog_id, (kind, row) in self.catalog.ids.items():
            if kind != "drink":
                continue
            try:
                icon = read_image(self.directory / "images" / "pDrinks" / f"{row['id']}.png")
            except (OSError, ValueError, RuntimeError):
                # 缺失竞争候选时，不能凭残缺图库确认身份。
                return
            vectors.append(self._feature(icon))
            ids.append(catalog_id)
        if vectors:
            self.vectors = np.stack(vectors)
            self.ids = ids

    def _identity(self, crop):
        if self.gallery_pending:
            self._load_gallery()
        if self.vectors is None:
            return None
        scores = self.vectors @ self._feature(crop)
        order = np.argsort(scores)
        best = order[-1]
        if scores[best] < .80 or len(order) > 1 and scores[best] - scores[order[-2]] < MATCH_MARGIN:
            return None
        catalog_id = self.ids[best]
        kind, row = self.catalog.ids[catalog_id]
        return {"catalog_id": catalog_id, "database_name": row["name"], "display_name": self.catalog.display_name(catalog_id),
                "reference_effect_dsl": self.catalog.dsl(kind, row), "similarity": round(float(scores[best]), 4)}

    def read(self, array, source_size):
        from maa.pipeline import JTemplateMatch, JRecognitionType

        # 三张模板对应瓶框外观。按实际位置分槽，第四格与前三格共用模板。
        hits = self.recognize(JRecognitionType.TemplateMatch, JTemplateMatch(
            template=[f"produce/drink_{i}.png" for i in (1, 2, 3)], roi=[10, 1150, 350, 100],
            threshold=[.8], green_mask=True,
        ), array)
        slots = {}
        for hit in sorted(hits, key=lambda hit: hit.score, reverse=True):
            if not math.isfinite(hit.score) or hit.score < .8:
                continue
            x, y, w, h = map(int, hit.box)
            slot = round((x - 27) / 85)
            if not 0 <= slot < 4 or abs(x - (27 + 85 * slot)) > 5 or abs(y - 1165) > 5 or slot in slots:
                continue
            # 去除圆框后，64 像素的内图对应图库中的完整方形图标。
            identity = self._identity(array[y + 3:y + h - 3, x + 3:x + w - 3])
            slots[slot] = {"slot": slot + 1, "box": scale_box(hit.box, PRODUCE_FRAME_SIZE, source_size), "identity": identity}
        return [slots[i] for i in sorted(slots)]


def locate_icons(image, region, sizes, operation=None):
    """用四条边的梯度支持定位方形图标；完整边框不足时保留未知。"""
    x, y, w, h = region
    mono = gray(image[y:y+h, x:x+w])
    edge = np.zeros(mono.shape, dtype=bool)
    edge[1:] |= np.abs(np.diff(mono, axis=0)) > 18
    edge[:, 1:] |= np.abs(np.diff(mono, axis=1)) > 18
    # 容许抗锯齿与 1–2 像素边框偏移。
    spread = edge.copy()
    for offset in (-2, -1, 1, 2):
        spread |= np.roll(edge, offset, axis=0) | np.roll(edge, offset, axis=1)
    integral = np.pad(spread.astype(np.int32), ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    proposals = []
    for size in sizes:
        yy, xx = np.mgrid[:h-size+1, :w-size+1]
        def area(dx, dy, width, height):
            left, top = xx + dx, yy + dy
            return (integral[top+height, left+width] - integral[top, left+width]
                    - integral[top+height, left] + integral[top, left]) / (width * height)
        sides = np.stack((area(0, 9, 1, size-18), area(size-1, 9, 1, size-18),
                          area(9, 0, size-18, 1), area(9, size-1, size-18, 1)))
        quality = sides.mean(axis=0)
        valid = (sides.min(axis=0) >= .25) & (quality >= .65)
        if operation in {"strengthen", "delete_copy_exchange"}:
            # 已核验的四列列表只在格子附近找边框，避免把卡面纹理当成整张图标。
            first_y = 697 if operation == "strengthen" else 398
            grid = np.zeros(valid.shape, dtype=bool)
            for row in range(3 if operation == "strengthen" else 4):
                for col in range(4):
                    grid |= (np.abs(xx + x - (80 + 147 * col)) <= 4) & (np.abs(yy + y - (first_y + 147 * row)) <= 4)
            valid &= grid
        vy, vx = np.nonzero(valid)
        for index in np.argsort(quality[valid])[::-1][:1500]:
            proposals.append((float(quality[vy[index], vx[index]]), [x+int(vx[index]), y+int(vy[index]), size, size]))
    boxes = []
    for _, box in sorted(proposals, key=lambda value: value[0], reverse=True):
        if not any(abs(box[0]-old[0]) < 35 and abs(box[1]-old[1]) < 35 for old in boxes):
            boxes.append(box)
    return sorted(boxes, key=lambda box: (round(box[1]/60), box[0]))


def icons_for_capture(directory, hashes=None, recognize=None, *, icon_resources=None):
    """从当前安装目录读取原始图；不依赖已删除的 detail 或 debug/cache。"""
    if icon_resources is not None and not any(row.get("complete", False) for row in icon_resources.values()):
        return None
    root = Path(__file__).resolve().parents[2]
    data = root / "data" if (root / "data").is_dir() else root / "assets/data"
    try:
        manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
        bindings = manifest.get("metadata", {}).get("bindings", {})
        name = bindings.get("configured_idol", {}).get("idol_name", "")
        name = "".join(name.split())
        idol_id = IDOL_NAMES.index(name) + 1 if name in IDOL_NAMES else None
        if hashes is None:
            hashes = {"skill_cards": hashlib.sha256((data / "produce_skill_cards.json").read_bytes()).hexdigest(),
                      "reward_items": hashlib.sha256((data / "produce_reward_items.json").read_bytes()).hexdigest()}
        return UpstreamIcons(data, idol_id, hashes, icon_resources=icon_resources, localized_names=bindings.get("language") == "zh-CN")
    except (OSError, ValueError, TypeError):
        return None
