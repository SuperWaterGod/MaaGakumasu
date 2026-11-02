"""只读上游图标匹配；所有缩放、遮罩与特征仅驻留内存，不生成模板文件。"""

import json
import hashlib
from pathlib import Path
from functools import lru_cache

import numpy as np
from utils.image_io import bgr_image, read_image, resize_image

ICON_SIZE = 128
ITEM_KINDS = {"skill_card", "drink", "p_item"}
MATCH_THRESHOLD = .70
MATCH_MARGIN = .05
GRAY_BRIGHTNESS = 120
# 上游 gakumas-data idols.json 的稳定 ID；未知偶像保留全部变体。
IDOL_NAMES = ("花海咲季", "月村手毬", "藤田ことね", "有村麻央", "葛城リーリヤ", "倉本千奈",
              "紫雲清夏", "篠澤広", "姫崎莉波", "花海佑芽", "十王星南", "秦谷美鈴", "雨夜燕")


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
    """初筛只定位卡名；角标和加号由后续原图差异区域单独核验。"""
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
                            "item_kind": kind, "icon_path": path.as_posix()})
            originals.append(gray(image).astype(np.uint8))
            vectors.append(feature(image, kind))
            pixels.append(normalized(gray(image)[identity_mask(kind)]))
        if entries:
            gallery[kind] = (entries, np.stack(vectors), np.stack(pixels), np.stack(originals))
    return gallery


class UpstreamIcons:
    def __init__(self, directory, idol_id=None, hashes=None):
        self.directory = Path(directory).resolve()
        self.idol_id = idol_id
        self.hashes = hashes
        self._last_scan = None

    def lookup(self, image, box, item_kind=None):
        """全流派初筛，每个身份至多两图精匹配，分数和分差均通过才确认具体版本。"""
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
            if item_kind and kind != item_kind:
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
            return None
        return {**entry, "similarity": round(score, 4), "catalog_hashes": self.hashes,
                "query_icon_sha256": hashlib.sha256(crop.tobytes()).hexdigest(), "method": "upstream_icon_variant"}

    def scan(self, image, operation):
        """仅观察已显示的完整图标，不增加输入，也不用 OCR 补牌名。"""
        regions = {"strengthen": (65, 690, 600, 420), "delete_copy_exchange": (65, 390, 600, 720),
                   "move_cards": (65, 390, 600, 720), "shop": (50, 465, 620, 345)}
        if operation not in regions:
            return []
        image = bgr_image(image)
        if image.shape != (1280, 720, 3):
            return []
        key = (operation, hashlib.sha256(image.tobytes()).digest())
        if self._last_scan and self._last_scan[0] == key:
            return [dict(hit) for hit in self._last_scan[1]]
        hits = []
        for box in locate_icons(image, regions[operation], (128,) if operation == "shop" else (120, 128), operation):
            match = self.lookup(image, box, None if operation == "shop" else "skill_card")
            if match:
                hits.append({**match, "box": box})
        self._last_scan = (key, hits)
        return [dict(hit) for hit in hits]


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


def icons_for_capture(directory, hashes=None, recognize=None):
    """从当前安装目录读取原始图；不依赖已删除的 detail 或 debug/cache。"""
    root = Path(__file__).resolve().parents[2]
    data = root / "data" if (root / "data").is_dir() else root / "assets/data"
    try:
        manifest = json.loads((Path(directory) / "manifest.json").read_text(encoding="utf-8"))
        name = manifest.get("metadata", {}).get("bindings", {}).get("configured_idol", {}).get("idol_name", "")
        name = "".join(name.split())
        idol_id = IDOL_NAMES.index(name) + 1 if name in IDOL_NAMES else None
        if hashes is None:
            hashes = {"skill_cards": hashlib.sha256((data / "produce_skill_cards.json").read_bytes()).hexdigest(),
                      "reward_items": hashlib.sha256((data / "produce_reward_items.json").read_bytes()).hexdigest()}
        return UpstreamIcons(data, idol_id, hashes)
    except (OSError, ValueError, TypeError):
        return None
