from tools.update_skill_cards import build_database


def test_build_database_preserves_variants_and_dsl_fields():
    rows = [
        {
            "id": 1,
            "name": "アピールの基本",
            "availableCustomizations": "",
            "rarity": "N",
            "type": "active",
            "plan": "free",
            "unlockPlv": 1,
            "upgraded": False,
            "unique": False,
            "sourceType": "default",
            "pIdolId": "",
            "forceInitialHand": False,
            "conditions": "",
            "cost": "cost-=4",
            "actions": "score+=9",
            "limit": "",
            "effects": "",
        },
        {
            "id": 2,
            "name": "アピールの基本+",
            "availableCustomizations": "65,68",
            "rarity": "N",
            "type": "active",
            "plan": "free",
            "unlockPlv": 1,
            "upgraded": True,
            "unique": False,
            "sourceType": "default",
            "pIdolId": "",
            "forceInitialHand": False,
            "conditions": "if:cost>=3",
            "cost": "cost-=3",
            "actions": "score+=14",
            "limit": 1,
            "effects": "at:turn { score+=1 }",
        },
    ]

    database = build_database(rows, source_path="fixture", source_revision="abc")

    assert database["source"]["record_count"] == 2
    normal, upgraded = database["cards"]
    assert normal["id"] == "skill_card_0001"
    assert normal["variant"] == "normal"
    assert upgraded["variant"] == "upgraded"
    assert upgraded["base_id"] == "skill_card_0001"
    assert upgraded["available_customizations"] == [65, 68]
    assert upgraded["effect_dsl"]["conditions"] == "if:cost>=3"
    assert upgraded["effect_dsl"]["effects"] == "at:turn { score+=1 }"
