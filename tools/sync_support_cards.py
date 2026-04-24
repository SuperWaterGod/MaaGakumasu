import urllib.request
import json
import os
import difflib

URL = "https://raw.githubusercontent.com/chinosk6/GakumasTranslationData/main/local-files/masterTrans/SupportCard.json"
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "assets", "data")
OUTPUT_FILE = os.path.join(OUTPUT_DIR, "support_cards.json")


def main():
    with urllib.request.urlopen(URL) as response:
        data = json.loads(response.read().decode("utf-8"))

    cards = data.get("data", [])

    result = [{"id": card["id"], "name": card["name"]} for card in cards]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"Saved {len(result)} support cards to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
