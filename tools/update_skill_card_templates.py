"""Export rectangular gameplay skill-card templates.

GkmasObjectManager is intentionally optional here. Use it to export/deobfuscate
game assets into a local directory, then point this tool at that directory.

Example:
    python tools/update_skill_card_templates.py --source-dir C:/tmp/gkmas-assets
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from PIL import Image, UnidentifiedImageError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_METADATA = REPO_ROOT / "assets" / "data" / "produce_skill_cards.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "assets" / "resource" / "base" / "image" / "produce" / "skill_cards"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MIN_GAMEPLAY_ASPECT = 1.15
MAX_GAMEPLAY_ASPECT = 2.20
SQUARE_TOLERANCE = 0.12


@dataclass(frozen=True)
class CardMeta:
    card_id: str
    source_id: int
    base_source_id: int
    name: str
    name_normalized: str
    variant: str


@dataclass(frozen=True)
class ImageCandidate:
    path: Path
    width: int
    height: int
    card_id: str
    variant: str
    reason: str

    @property
    def key(self) -> tuple[str, str]:
        return self.card_id, self.variant


def _slug(value: str) -> str:
    value = value.lower().replace("+", "plus")
    return re.sub(r"[^0-9a-z\u3040-\u30ff\u3400-\u9fff]+", "", value)


def load_cards(metadata_path: Path = DEFAULT_METADATA) -> list[CardMeta]:
    with metadata_path.open("r", encoding="utf-8") as fp:
        database = json.load(fp)
    cards = []
    for row in database.get("cards", []):
        cards.append(
            CardMeta(
                card_id=str(row["id"]),
                source_id=int(row["source_id"]),
                base_source_id=int(row.get("base_source_id", row["source_id"])),
                name=str(row.get("name", "")),
                name_normalized=str(row.get("name_normalized", row.get("name", ""))),
                variant=str(row.get("variant", "normal")),
            )
        )
    return cards


def iter_manifest_paths(manifest_path: Path, source_dir: Optional[Path]) -> Iterable[Path]:
    with manifest_path.open("r", encoding="utf-8") as fp:
        manifest = json.load(fp)
    entries = manifest if isinstance(manifest, list) else manifest.values() if isinstance(manifest, dict) else []
    for entry in entries:
        if isinstance(entry, str):
            raw_path = entry
        elif isinstance(entry, dict):
            raw_path = (
                entry.get("path")
                or entry.get("file")
                or entry.get("filename")
                or entry.get("assetPath")
                or entry.get("name")
                or entry.get("resource")
            )
        else:
            raw_path = None
        if not raw_path:
            continue
        path = Path(str(raw_path))
        if not path.is_absolute() and source_dir:
            path = source_dir / path
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            yield path


def iter_image_paths(source_dir: Optional[Path], manifest_path: Optional[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    if manifest_path:
        for path in iter_manifest_paths(manifest_path, source_dir):
            resolved = path.resolve()
            if resolved not in seen and resolved.exists():
                seen.add(resolved)
                yield resolved
    if source_dir and source_dir.exists():
        for path in source_dir.rglob("*"):
            if path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield resolved


def _is_rectangular_gameplay_card(width: int, height: int) -> tuple[bool, str]:
    if width <= 0 or height <= 0:
        return False, "invalid_dimensions"
    if abs(width - height) / max(width, height) <= SQUARE_TOLERANCE:
        return False, "square_image"
    aspect = height / width
    if aspect < MIN_GAMEPLAY_ASPECT or aspect > MAX_GAMEPLAY_ASPECT:
        return False, "non_gameplay_aspect"
    return True, "rectangular_gameplay_aspect"


def _variant_hint(path: Path) -> Optional[str]:
    text = _slug(" ".join(path.parts[-4:]))
    if "upgraded" in text or "upgrade" in text or "plus" in text:
        return "upgraded"
    if "normal" in text or "base" in text:
        return "normal"
    return None


def _match_cards(path: Path, cards: list[CardMeta]) -> list[tuple[CardMeta, str]]:
    text = _slug(" ".join(path.parts[-5:]))
    stem_numbers = {int(value) for value in re.findall(r"\d+", path.stem)}
    matches: list[tuple[CardMeta, str]] = []
    for card in cards:
        numeric_tokens = {
            f"{card.source_id:04d}",
            card.card_id,
            card.card_id.replace("_", ""),
        }
        if card.source_id in stem_numbers or any(_slug(token) in text for token in numeric_tokens if token):
            matches.append((card, "source_id"))
            continue
        name_tokens = {_slug(card.name), _slug(card.name_normalized)}
        if any(token and token in text for token in name_tokens):
            matches.append((card, "name"))
    variant = _variant_hint(path)
    if variant:
        matches = [(card, reason) for card, reason in matches if card.variant == variant]
    return matches


def discover_candidates(
    image_paths: Iterable[Path],
    cards: list[CardMeta],
) -> tuple[list[ImageCandidate], dict[str, list[str]]]:
    candidates: list[ImageCandidate] = []
    rejected: dict[str, list[str]] = {"square_image": [], "non_gameplay_aspect": [], "unknown_card": [], "ambiguous_asset_name": []}
    for path in image_paths:
        try:
            with Image.open(path) as image:
                width, height = image.size
        except (OSError, UnidentifiedImageError):
            continue
        accepted, reason = _is_rectangular_gameplay_card(width, height)
        if not accepted:
            rejected.setdefault(reason, []).append(str(path))
            continue
        matches = _match_cards(path, cards)
        if not matches:
            rejected["unknown_card"].append(str(path))
            continue
        unique = {(card.card_id, card.variant): (card, match_reason) for card, match_reason in matches}
        if len(unique) != 1:
            rejected["ambiguous_asset_name"].append(str(path))
            continue
        (card_id, variant), (_, match_reason) = next(iter(unique.items()))
        candidates.append(ImageCandidate(path=path, width=width, height=height, card_id=card_id, variant=variant, reason=match_reason))
    return candidates, rejected


def _copy_candidate(candidate: ImageCandidate, output_dir: Path) -> Path:
    target = output_dir / candidate.card_id / f"{candidate.variant}.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(candidate.path) as image:
        image.save(target)
    return target


def export_templates(candidates: list[ImageCandidate], cards: list[CardMeta], output_dir: Path, *, dry_run: bool = False) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[ImageCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.key, []).append(candidate)

    exported = []
    ambiguous = []
    expected_keys = {(card.card_id, card.variant) for card in cards}
    for key in sorted(grouped):
        group = grouped[key]
        if len(group) != 1:
            ambiguous.append({"card_id": key[0], "variant": key[1], "paths": [str(item.path) for item in group]})
            continue
        target = output_dir / key[0] / f"{key[1]}.png"
        if not dry_run:
            target = _copy_candidate(group[0], output_dir)
        exported.append(
            {
                "card_id": key[0],
                "variant": key[1],
                "source": str(group[0].path),
                "target": str(target),
                "size": [group[0].width, group[0].height],
                "match_reason": group[0].reason,
            }
        )

    exported_keys = {(item["card_id"], item["variant"]) for item in exported}
    ambiguous_keys = {(item["card_id"], item["variant"]) for item in ambiguous}
    missing = [{"card_id": card_id, "variant": variant} for card_id, variant in sorted(expected_keys - exported_keys - ambiguous_keys)]
    return {"exported": exported, "ambiguous": ambiguous, "missing": missing}


def write_report(report: dict[str, Any], report_path: Path) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("w", encoding="utf-8", newline="\n") as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2)
        fp.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Install and run GkmasObjectManager separately to export game assets. "
            "This tool only filters rectangular gameplay-card images, maps them to gakumas-data ids, "
            "and writes canonical templates plus a report."
        ),
    )
    parser.add_argument("--metadata", default=str(DEFAULT_METADATA), help="produce_skill_cards.json path.")
    parser.add_argument("--source-dir", help="Directory of images exported by GkmasObjectManager.")
    parser.add_argument("--manifest", help="Optional GkmasObjectManager/export manifest JSON.")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Output template directory.")
    parser.add_argument("--report", default=str(REPO_ROOT / "assets" / "data" / "produce_skill_card_templates_report.json"))
    parser.add_argument("--dry-run", action="store_true", help="Do not write template images.")
    parser.add_argument("--clear-output", action="store_true", help="Clear the output directory before exporting.")
    args = parser.parse_args()

    metadata_path = Path(args.metadata).resolve()
    source_dir = Path(args.source_dir).resolve() if args.source_dir else None
    manifest_path = Path(args.manifest).resolve() if args.manifest else None
    output_dir = Path(args.output_dir).resolve()

    cards = load_cards(metadata_path)
    if args.clear_output and output_dir.exists() and not args.dry_run:
        shutil.rmtree(output_dir)

    image_paths = list(iter_image_paths(source_dir, manifest_path))
    candidates, rejected = discover_candidates(image_paths, cards)
    report = export_templates(candidates, cards, output_dir, dry_run=args.dry_run)
    report["metadata"] = {
        "cards": len(cards),
        "image_paths_scanned": len(image_paths),
        "source_dir": str(source_dir) if source_dir else None,
        "manifest": str(manifest_path) if manifest_path else None,
        "output_dir": str(output_dir),
        "dry_run": args.dry_run,
    }
    report["rejected"] = rejected
    write_report(report, Path(args.report).resolve())
    print(
        "Exported {exported}, ambiguous {ambiguous}, missing {missing}; report: {report}".format(
            exported=len(report["exported"]),
            ambiguous=len(report["ambiguous"]),
            missing=len(report["missing"]),
            report=Path(args.report).resolve(),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
