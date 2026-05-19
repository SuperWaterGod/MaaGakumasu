import sys
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "agent"))
MODULE_PATH = REPO_ROOT / "agent" / "custom" / "action" / "skill_cards.py"
SPEC = importlib.util.spec_from_file_location("skill_cards", MODULE_PATH)
skill_cards = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(skill_cards)


def test_identify_card_by_name_matches_ocr_noise(monkeypatch):
    monkeypatch.setattr(
        skill_cards,
        "load_skill_cards",
        lambda: [
            {"id": "skill_card_0001", "name": "アピールの基本", "name_normalized": "アピールの基本", "variant": "normal"},
            {"id": "skill_card_0002", "name": "ポーズの基本+", "name_normalized": "ポーズの基本", "variant": "upgraded"},
        ],
    )

    match = skill_cards.identify_card_by_name(" アピール の 基本 ")

    assert match is not None
    card, score = match
    assert card["id"] == "skill_card_0001"
    assert score >= 0.9


def test_first_existing_path_uses_packaged_data_layout(tmp_path):
    source_path = tmp_path / "assets" / "data" / "produce_skill_cards.json"
    install_path = tmp_path / "data" / "produce_skill_cards.json"
    install_path.parent.mkdir(parents=True)
    install_path.write_text("{}", encoding="utf-8")

    assert skill_cards._first_existing_path(source_path, install_path) == install_path


def test_merge_detection_results_preserves_unknown_slots():
    class Result:
        def __init__(self, label, box, score):
            self.label = label
            self.box = box
            self.score = score

    merged = skill_cards._merge_detection_results(
        [
            Result("cards", [100, 900, 120, 180], 0.8),
            Result("suggestions", [102, 902, 120, 180], 0.7),
            Result("useless", [300, 900, 120, 180], 0.6),
        ]
    )

    assert len(merged) == 2
    assert merged[0]["labels"] == {"cards", "suggestions"}
    assert merged[1]["labels"] == {"useless"}
