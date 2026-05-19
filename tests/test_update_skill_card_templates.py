from pathlib import Path

from PIL import Image

from tools.update_skill_card_templates import CardMeta, discover_candidates, export_templates


def _save_image(path: Path, size: tuple[int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, "white").save(path)


def test_template_discovery_rejects_square_and_exports_rectangular(tmp_path):
    rect = tmp_path / "assets" / "skill_card_0001_normal.png"
    square = tmp_path / "assets" / "skill_card_0002_normal.png"
    _save_image(rect, (200, 330))
    _save_image(square, (256, 256))
    cards = [
        CardMeta("skill_card_0001", 1, 1, "カードA", "カードA", "normal"),
        CardMeta("skill_card_0002", 2, 2, "カードB", "カードB", "normal"),
    ]

    candidates, rejected = discover_candidates([rect, square], cards)
    report = export_templates(candidates, cards, tmp_path / "out")

    assert len(candidates) == 1
    assert candidates[0].card_id == "skill_card_0001"
    assert str(square) in rejected["square_image"]
    assert len(report["exported"]) == 1
    assert report["missing"] == [{"card_id": "skill_card_0002", "variant": "normal"}]
    assert (tmp_path / "out" / "skill_card_0001" / "normal.png").exists()


def test_template_export_reports_ambiguous_card(tmp_path):
    first = tmp_path / "a" / "skill_card_0001_normal.png"
    second = tmp_path / "b" / "skill_card_0001_normal.png"
    _save_image(first, (200, 330))
    _save_image(second, (220, 360))
    cards = [CardMeta("skill_card_0001", 1, 1, "カードA", "カードA", "normal")]

    candidates, rejected = discover_candidates([first, second], cards)
    report = export_templates(candidates, cards, tmp_path / "out")

    assert not rejected["square_image"]
    assert len(report["ambiguous"]) == 1
    assert report["ambiguous"][0]["card_id"] == "skill_card_0001"
