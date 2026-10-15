"""从培育实验 HUD 原图裁剪像素，遮罩变化区域和背景，生成 MaaFramework 模板。"""

import json
import hashlib
import argparse
from pathlib import Path

from PIL import Image, ImageDraw


def build(manifest_path, capture_root, image_root, preview=None):
    manifest_path, capture_root, image_root = (Path(path).resolve() for path in (manifest_path, capture_root, image_root))
    registry = json.loads(manifest_path.read_text(encoding="utf-8"))
    crops = []
    for entry in [*registry["buffs"], *registry.get("stances", []), *registry.get("stance_previews", [])]:
        source = entry["source"]
        frame = (capture_root / source["run_id"] / "frames" / (source["frame_sha256"] + ".png")).resolve()
        if not frame.is_relative_to(capture_root):
            raise ValueError("Source frame escaped capture directory")
        content = frame.read_bytes()
        if hashlib.sha256(content).hexdigest() != source["frame_sha256"]:
            raise ValueError(f"Source checksum mismatch: {frame}")
        with Image.open(frame) as image:
            if image.size != (720, 1280):
                raise ValueError("Templates require a 720x1280 source; no resizing or rotation")
            left, top, width, height = source["box"]
            if left < 0 or top < 0 or left + width > image.width or top + height > image.height:
                raise ValueError("Source crop escaped frame")
            original = image.convert("RGB").crop((left, top, left + width, top + height))
        template = original.copy()
        mask = entry["mask"]
        cx, cy = mask["center"]
        pixels = template.load()
        for y in range(height):
            for x in range(width):
                if abs(x - cx) + abs(y - cy) > mask["radius"]:
                    pixels[x, y] = (0, 255, 0)
        draw = ImageDraw.Draw(template)
        for x, y, w, h in mask.get("rectangles", []):
            draw.rectangle((x, y, x + w - 1, y + h - 1), fill=(0, 255, 0))
        target = (image_root / entry["template"]).resolve()
        if not target.is_relative_to(image_root):
            raise ValueError("Template escaped image directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        template.save(target, format="PNG")
        entry["template_sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        crops.append((entry["id"], original, template))
        print(f"{entry['id']}: {target}")
    manifest_path.write_text(json.dumps(registry, ensure_ascii=False, indent=4) + "\n", encoding="utf-8")
    if preview:
        sheet = Image.new("RGB", (720, ((len(crops) + 3) // 4) * 105), "#eeeeee")
        draw = ImageDraw.Draw(sheet)
        for index, (name, original, template) in enumerate(crops):
            x, y = index % 4 * 180, index // 4 * 105
            draw.text((x + 5, y + 5), name, fill="black")
            sheet.paste(original, (x + 8, y + 28))
            sheet.paste(template, (x + 85, y + 28))
        preview = Path(preview)
        preview.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(preview)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("assets/data/battle_buff_templates.json"))
    parser.add_argument("--capture-root", type=Path, required=True)
    parser.add_argument("--image-root", type=Path, default=Path("assets/resource/base/image"))
    parser.add_argument("--preview", type=Path)
    args = parser.parse_args()
    build(args.manifest, args.capture_root, args.image_root, args.preview)


if __name__ == "__main__":
    main()
