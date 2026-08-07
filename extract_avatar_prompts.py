import json
import os
import sys

# ===========================
# CONFIG
# ===========================

JSON_FILE = "prompts.json"
OUTPUT_FILE = "avatar-prompt.json"

TARGET_TYPE = "AVATAR"


# ===========================
# MAIN
# ===========================

def main():
    if not os.path.exists(JSON_FILE):
        sys.exit(f"Missing {JSON_FILE} in {os.getcwd()}")

    with open(JSON_FILE, "r", encoding="utf8") as f:
        items = json.load(f)

    avatars = []
    skipped = 0

    for item in items:
        if str(item.get("type", "")).upper() != TARGET_TYPE:
            continue

        narration = (item.get("narration") or "").strip()
        if not narration:
            # An AVATAR beat with nothing to say is a data error, not output.
            skipped += 1
            print(f"  item {item.get('i')}: AVATAR with empty narration — skipped")
            continue

        # Timings come along so the narration can be lined back up with the
        # edit later; image_prompt is always null on these beats.
        avatars.append({
            "i": item.get("i"),
            "t_start": item.get("t_start"),
            "t_end": item.get("t_end"),
            "narration": narration,
        })

    with open(OUTPUT_FILE, "w", encoding="utf8") as f:
        json.dump(avatars, f, indent=2, ensure_ascii=False)
        f.write("\n")

    words = sum(len(a["narration"].split()) for a in avatars)
    print(f"\nRead {len(items)} items from {JSON_FILE}.")
    print(f"Wrote {len(avatars)} {TARGET_TYPE} narrations ({words} words) "
          f"-> {OUTPUT_FILE}")

    if skipped:
        print(f"{skipped} {TARGET_TYPE} item(s) had no narration.")


if __name__ == "__main__":
    main()
