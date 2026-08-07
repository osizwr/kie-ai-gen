"""Step 2 of the pipeline: BROLL image + narration -> LTX motion prompt.

Replaces the manual Gemini chat: for every BROLL beat whose image already
exists in images/, send engineer2_motion_vision.txt as the system prompt and
the image + narration as the message, then collect the reply.

Run AFTER generate_images.py. Safe to re-run — beats already in the output
file are skipped, so it picks up where it left off as more images land.
"""

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

# Image naming has to match whatever wrote the files, so borrow it rather
# than re-deriving it here.
from generate_images import already_done, image_name

# ===========================
# CONFIG
# ===========================

load_dotenv()

API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.1-pro-preview")

JSON_FILE = "prompts.json"
SYSTEM_PROMPT_FILE = "engineer2_motion_vision.txt"
OUTPUT_FILE = "motion-prompt.json"
LOG_FILE = "motion_prompts.log"

TARGET_TYPE = "BROLL"

CONCURRENCY = 3          # parallel requests in flight
MAX_RETRIES = 4          # per API call
RETRY_CAP = 90           # longest we'll honour a server-supplied retryDelay
TEMPERATURE = 0.7

MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

print_lock = threading.Lock()
results_lock = threading.Lock()

# Tripped by a failure that every other beat would hit too (no quota for the
# model), so the run stops instead of repeating itself 45 times.
abort = threading.Event()


class NoQuota(RuntimeError):
    pass


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
# GEMINI CALL
# ===========================

def retry_after(e):
    """A 429 carries the server's own retryDelay — honour it over guessing."""

    details = getattr(e, "details", None) or {}

    for detail in (details.get("error") or {}).get("details") or []:
        delay = detail.get("retryDelay")
        if delay:
            try:
                return float(str(delay).rstrip("s"))
            except ValueError:
                return None

    return None


def no_quota(e):
    """`limit: 0` means this key has no allowance for this model at all.

    That's what a free-tier key hitting a Pro model looks like, and no amount
    of backing off will change it — so fail loudly instead of retrying.
    """

    return "limit: 0" in str(getattr(e, "message", "") or "")


def ask_gemini(client, system_prompt, image_path, narration):
    """One vision call, retried on rate limits and server-side blips."""

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    mime = MIME_TYPES.get(os.path.splitext(image_path)[1].lower(), "image/png")

    contents = [
        types.Part.from_bytes(data=image_bytes, mime_type=mime),
        f"NARRATION: {narration}",
    ]
    config = types.GenerateContentConfig(
        system_instruction=system_prompt,
        temperature=TEMPERATURE,
    )

    delay = 2

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=MODEL, contents=contents, config=config
            )

        except errors.APIError as e:
            if no_quota(e):
                raise NoQuota(
                    f"{MODEL} has no quota on this API key (free tier allows 0 "
                    f"requests for Pro models). Enable billing on the Google "
                    f"Cloud project, or set GEMINI_MODEL in .env to a flash model."
                ) from e

            code = getattr(e, "code", None)
            transient = code == 429 or (isinstance(code, int) and code >= 500)

            if not transient or attempt == MAX_RETRIES:
                raise

            time.sleep(min(retry_after(e) or delay, RETRY_CAP))
            delay *= 2
            continue

        text = (response.text or "").strip()
        if not text:
            # Usually a safety block or an empty candidate — worth one more try.
            if attempt == MAX_RETRIES:
                raise RuntimeError("empty response from model")
            time.sleep(delay)
            delay *= 2
            continue

        return text


# ===========================
# WORKER
# ===========================

def process(client, system_prompt, index, total, item):
    image_id = image_name(item, index)
    label = f"[{index}/{total}] {image_id}"

    if abort.is_set():
        return "aborted", None

    image_path = already_done(image_id)
    if not image_path:
        log(f"{label} no image yet — run generate_images.py first")
        return "no_image", None

    try:
        motion = ask_gemini(client, system_prompt, image_path, item["narration"])
    except NoQuota as e:
        if not abort.is_set():
            abort.set()
            log(f"{label} ERROR: {e}")
            log("Stopping — every other beat would fail the same way.")
        return "aborted", None
    except Exception as e:
        log(f"{label} ERROR: {e}")
        return "failed", None

    log(f"{label} -> {motion[:70]}{'...' if len(motion) > 70 else ''}")

    return "ok", {
        "i": item.get("i"),
        "t_start": item.get("t_start"),
        "t_end": item.get("t_end"),
        "format": item.get("format"),
        "narration": item["narration"],
        "image": image_path,
        "motion_prompt": motion,
    }


# ===========================
# OUTPUT
# ===========================

def load_existing():
    """Previous results, keyed by beat number, so a re-run resumes."""

    if not os.path.exists(OUTPUT_FILE):
        return {}

    try:
        with open(OUTPUT_FILE, "r", encoding="utf8") as f:
            return {r["i"]: r for r in json.load(f)}
    except (ValueError, KeyError, TypeError):
        log(f"{OUTPUT_FILE} is unreadable — starting a fresh one")
        return {}


def save(records):
    """Written after every success: an interrupted run keeps what it paid for."""

    ordered = sorted(records.values(), key=lambda r: (r["i"] is None, r["i"]))
    tmp = OUTPUT_FILE + ".part"

    with open(tmp, "w", encoding="utf8") as f:
        json.dump(ordered, f, indent=2, ensure_ascii=False)
        f.write("\n")

    os.replace(tmp, OUTPUT_FILE)


# ===========================
# MAIN
# ===========================

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", action="store_true",
                        help="list the model ids this API key can use, then exit")
    parser.add_argument("--redo", action="store_true",
                        help="regenerate every beat, ignoring existing results")
    args = parser.parse_args()

    if not API_KEY:
        sys.exit("No API key. Add GEMINI_API_KEY to .env "
                 "(get one at https://aistudio.google.com/apikey).")

    client = genai.Client(api_key=API_KEY)

    if args.models:
        for m in client.models.list():
            actions = getattr(m, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                print(m.name)
        return

    for path in (JSON_FILE, SYSTEM_PROMPT_FILE):
        if not os.path.exists(path):
            sys.exit(f"Missing {path} in {os.getcwd()}")

    with open(SYSTEM_PROMPT_FILE, "r", encoding="utf8") as f:
        system_prompt = f.read()

    with open(JSON_FILE, "r", encoding="utf8") as f:
        items = json.load(f)

    broll = [i for i in items if str(i.get("type", "")).upper() == TARGET_TYPE]
    total = len(broll)

    records = {} if args.redo else load_existing()

    todo = [
        (index, item)
        for index, item in enumerate(broll, start=1)
        if (item.get("narration") or "").strip()
        and item.get("i") not in records
    ]

    log(f"\n=== Run started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    log(f"{len(items)} items, {total} {TARGET_TYPE}. "
        f"{len(records)} already done, {len(todo)} to do. "
        f"Model={MODEL} Concurrency={CONCURRENCY}\n")

    counts = {"ok": 0, "no_image": 0, "failed": 0, "aborted": 0}

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [
            pool.submit(process, client, system_prompt, index, total, item)
            for index, item in todo
        ]
        for fut in as_completed(futures):
            status, record = fut.result()
            counts[status] += 1

            if record:
                with results_lock:
                    records[record["i"]] = record
                    save(records)

    save(records)

    log(f"\nFinished. {counts['ok']} written, {counts['no_image']} waiting on "
        f"images, {counts['failed']} failed. "
        f"{len(records)}/{total} total in {OUTPUT_FILE}")

    if counts["aborted"]:
        log(f"{counts['aborted']} beat(s) never attempted — fix the model or "
            f"billing, then re-run. Nothing already written is lost.")

    if counts["no_image"]:
        log("Run generate_images.py to fill the gaps, then re-run this.")

    if counts["failed"]:
        log(f"Re-run to retry failures — finished beats are skipped. "
            f"Details in {LOG_FILE} (grep ERROR).")


if __name__ == "__main__":
    main()
