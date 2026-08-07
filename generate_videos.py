"""Step 3 of the pipeline: BROLL image + motion prompt -> 6s video clip.

Reads motion-prompt.json (written by generate_motion_prompts.py) and runs each
beat through LTX-2.3-fast on Replicate: the generated image as the starting
frame, the motion prompt as the prompt, a random camera move per clip.

Run AFTER generate_motion_prompts.py. Safe to re-run — clips already on disk
are skipped, so it picks up where it left off.

Replicate bills per run, so start with --limit 2 before committing to all 45.
"""

import argparse
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import replicate
import requests
from dotenv import load_dotenv
from replicate.exceptions import ModelError, ReplicateError

# ===========================
# CONFIG
# ===========================

load_dotenv()

API_TOKEN = os.environ.get("REPLICATE_API_TOKEN", "")

MODEL = "lightricks/ltx-2.3-fast"

INPUT_FILE = "motion-prompt.json"
OUTPUT_FOLDER = "videos"
MANIFEST_FILE = "video-manifest.json"
LOG_FILE = "generate_videos.log"

RESOLUTION = "1080p"
DURATION = 6              # seconds; 6/8/10/12/14/16/18/20
ASPECT_RATIO = "16:9"     # matches what generate_images.py asked KIE for
FPS = 25
GENERATE_AUDIO = False    # the edit carries its own narration

# The model's own enum. Deliberately excludes static/focus_shift/none — every
# clip gets a real move.
CAMERA_MOTIONS = [
    "dolly_left",
    "dolly_right",
    "dolly_in",
    "dolly_out",
    "jib_up",
    "jib_down",
]

CONCURRENCY = 3
MAX_RETRIES = 3

print_lock = threading.Lock()
manifest_lock = threading.Lock()


def log(msg):
    """Console stays clean; the file gets timestamps and survives the terminal."""

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    with print_lock:
        print(msg, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf8") as f:
                for line in str(msg).splitlines() or [""]:
                    f.write(f"{stamp} {line}\n" if line else "\n")
        except OSError:
            pass  # never let logging kill a run that's otherwise fine


# ===========================
# CAMERA MOVES
# ===========================

def assign_motions(records, rng):
    """A random move per clip, never the same one twice in a row.

    Pure random would happily hand two neighbouring beats the same dolly-in,
    which reads as a mistake once they're cut together.
    """

    motions = []
    previous = None

    for _ in records:
        pick = rng.choice([m for m in CAMERA_MOTIONS if m != previous])
        motions.append(pick)
        previous = pick

    return motions


# ===========================
# GENERATE / SAVE
# ===========================

def save_video(output, path):
    """Atomic: write to .part, then rename, so partials never look complete."""

    if isinstance(output, list):
        output = output[0]

    tmp = path + ".part"

    with open(tmp, "wb") as f:
        if hasattr(output, "read"):
            f.write(output.read())
        else:
            with requests.get(str(output), stream=True, timeout=600) as r:
                r.raise_for_status()
                for chunk in r.iter_content(8192):
                    f.write(chunk)

    if os.path.getsize(tmp) == 0:
        os.remove(tmp)
        raise RuntimeError("downloaded 0 bytes")

    os.replace(tmp, path)


def run_model(image_path, prompt, motion):
    """One prediction, retried on Replicate-side blips."""

    delay = 5

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            with open(image_path, "rb") as image:
                return replicate.run(
                    MODEL,
                    input={
                        "prompt": prompt,
                        "image": image,
                        "resolution": RESOLUTION,
                        "duration": DURATION,
                        "aspect_ratio": ASPECT_RATIO,
                        "fps": FPS,
                        "camera_motion": motion,
                        "generate_audio": GENERATE_AUDIO,
                    },
                )

        except ModelError:
            # The model ran and refused this input; the same input will refuse
            # again, so don't pay for the retry.
            raise

        except (ReplicateError, requests.RequestException):
            if attempt == MAX_RETRIES:
                raise
            time.sleep(delay)
            delay *= 2


def video_path_for(record):
    """`images/12 - BROLL.png` -> `videos/12 - BROLL.mp4`, keeping the pairing."""

    stem = os.path.splitext(os.path.basename(record["image"]))[0]
    return os.path.join(OUTPUT_FOLDER, stem + ".mp4")


def process(index, total, record, motion):
    path = video_path_for(record)
    label = f"[{index}/{total}] {os.path.basename(path)}"

    if os.path.exists(path):
        log(f"{label} exists -> {path}")
        return "skipped", None

    if not os.path.exists(record["image"]):
        log(f"{label} ERROR: missing image {record['image']}")
        return "failed", None

    started = time.monotonic()

    try:
        output = run_model(record["image"], record["motion_prompt"], motion)
        save_video(output, path)
    except Exception as e:
        log(f"{label} ERROR: {e}")
        return "failed", None

    size = os.path.getsize(path) / 1_000_000
    log(f"{label} {motion} {time.monotonic() - started:.0f}s "
        f"{size:.1f}MB -> {path}")

    return "ok", {
        "i": record.get("i"),
        "video": path,
        "image": record["image"],
        "camera_motion": motion,
        "motion_prompt": record["motion_prompt"],
    }


# ===========================
# MANIFEST
# ===========================

def load_manifest():
    if not os.path.exists(MANIFEST_FILE):
        return {}

    try:
        with open(MANIFEST_FILE, "r", encoding="utf8") as f:
            return {r["i"]: r for r in json.load(f)}
    except (ValueError, KeyError, TypeError):
        log(f"{MANIFEST_FILE} is unreadable — starting a fresh one")
        return {}


def save_manifest(records):
    """Which random move each clip got — otherwise it's unrecoverable."""

    ordered = sorted(records.values(), key=lambda r: (r["i"] is None, r["i"]))
    tmp = MANIFEST_FILE + ".part"

    with open(tmp, "w", encoding="utf8") as f:
        json.dump(ordered, f, indent=2, ensure_ascii=False)
        f.write("\n")

    os.replace(tmp, MANIFEST_FILE)


# ===========================
# MAIN
# ===========================

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, metavar="N",
                        help="only do the first N clips — use this first, "
                             "Replicate bills per run")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be submitted, call nothing")
    parser.add_argument("--motion", choices=CAMERA_MOTIONS,
                        help="force one camera move instead of choosing randomly")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed the camera-move picker for a repeatable run")
    args = parser.parse_args()

    if not args.dry_run and not API_TOKEN:
        sys.exit("No API token. Add REPLICATE_API_TOKEN to .env "
                 "(get one at https://replicate.com/account/api-tokens).")

    if not os.path.exists(INPUT_FILE):
        sys.exit(f"Missing {INPUT_FILE} — run generate_motion_prompts.py first.")

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    with open(INPUT_FILE, "r", encoding="utf8") as f:
        records = json.load(f)

    records = [r for r in records if (r.get("motion_prompt") or "").strip()]
    records.sort(key=lambda r: (r.get("i") is None, r.get("i")))

    # Assigned in beat order before anything is submitted, so the no-repeat
    # rule follows the timeline rather than whatever order threads finish in.
    rng = random.Random(args.seed)
    motions = ([args.motion] * len(records) if args.motion
               else assign_motions(records, rng))

    todo = list(zip(records, motions))
    if args.limit:
        todo = todo[:args.limit]

    total = len(todo)

    log(f"\n=== Run started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    log(f"{len(records)} clips available, doing {total}. "
        f"{MODEL} {RESOLUTION} {DURATION}s {FPS}fps {ASPECT_RATIO} "
        f"audio={'on' if GENERATE_AUDIO else 'off'} Concurrency={CONCURRENCY}\n")

    if args.dry_run:
        for index, (record, motion) in enumerate(todo, start=1):
            path = video_path_for(record)
            mark = "SKIP (exists)" if os.path.exists(path) else motion
            log(f"[{index}/{total}] {os.path.basename(path)} {mark}")
            log(f"    image  {record['image']}")
            log(f"    prompt {record['motion_prompt'][:100]}...")
        log(f"\nDry run — nothing submitted, nothing billed.")
        return

    manifest = load_manifest()
    counts = {"ok": 0, "skipped": 0, "failed": 0}

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [
            pool.submit(process, index, total, record, motion)
            for index, (record, motion) in enumerate(todo, start=1)
        ]
        for fut in as_completed(futures):
            status, entry = fut.result()
            counts[status] += 1

            if entry:
                with manifest_lock:
                    manifest[entry["i"]] = entry
                    save_manifest(manifest)

    log(f"\nFinished. {counts['ok']} generated, {counts['skipped']} skipped, "
        f"{counts['failed']} failed. {len(manifest)} clips in {MANIFEST_FILE}")

    if counts["failed"]:
        log(f"Re-run to retry failures — finished clips are skipped. "
            f"Details in {LOG_FILE} (grep ERROR).")


if __name__ == "__main__":
    main()
