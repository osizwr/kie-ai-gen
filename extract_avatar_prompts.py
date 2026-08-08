import json
import os
import sys
from collections import Counter

# ===========================
# CONFIG
# ===========================

JSON_FILE = "prompts.json"
OUTPUT_FILE = "avatar-prompt.json"

TARGET_TYPES = ("AVATAR", "SPLIT")


# ===========================
# MAIN
# ===========================

def main():
    if not os.path.exists(JSON_FILE):
        sys.exit(f"Missing {JSON_FILE} in {os.getcwd()}")

    with open(JSON_FILE, "r", encoding="utf8") as f:
        items = json.load(f)

    narrations = []
    skipped = 0

    for item in items:
        kind = str(item.get("type", "")).upper()
        if kind not in TARGET_TYPES:
            continue

        narration = (item.get("narration") or "").strip()
        if not narration:
            # A beat with nothing to say is a data error, not output.
            skipped += 1
            print(f"  item {item.get('i')}: {kind} with empty narration — skipped")
            continue

        # Timings come along so the narration can be lined back up with the
        # edit later. The type rides along too — AVATAR beats are narration
        # only, SPLIT beats also have a picture, and the two are read
        # differently downstream.
        narrations.append({
            "i": item.get("i"),
            "t_start": item.get("t_start"),
            "t_end": item.get("t_end"),
            "type": kind,
            "narration": narration,
        })

    with open(OUTPUT_FILE, "w", encoding="utf8") as f:
        json.dump(narrations, f, indent=2, ensure_ascii=False)
        f.write("\n")

    words = sum(len(n["narration"].split()) for n in narrations)
    counts = Counter(n["type"] for n in narrations)
    breakdown = ", ".join(f"{counts[t]} {t}" for t in TARGET_TYPES)

    print(f"\nRead {len(items)} items from {JSON_FILE}.")
    print(f"Wrote {len(narrations)} narrations ({breakdown}, {words} words) "
          f"-> {OUTPUT_FILE}")

    if skipped:
        print(f"{skipped} item(s) had no narration.")


if __name__ == "__main__":
    main()
