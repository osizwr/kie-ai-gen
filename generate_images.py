import json
import os
import re
import shutil
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

# ===========================
# CONFIG
# ===========================

load_dotenv()

API_KEY = os.environ.get("KIE_API_KEY", "")

JSON_FILE = "prompts.json"
OUTPUT_FOLDER = "images"
LOG_FILE = "generate_images.log"

ASPECT_RATIO = "16:9"
RESOLUTION = "2K"

CONCURRENCY = 3          # parallel images in flight
POLL_INTERVAL = 5        # seconds between status checks
TASK_TIMEOUT = 600       # give up on a single image after 10 min
MAX_RETRIES = 4          # per HTTP call
TASK_ATTEMPTS = 3        # resubmits when generation fails server-side

CREATE_URL = "https://api.kie.ai/api/v1/jobs/createTask"
STATUS_URL = "https://api.kie.ai/api/v1/jobs/recordInfo"

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

print_lock = threading.Lock()


def log(msg):
    """Console stays clean; the file gets timestamps and survives the terminal.

    Reopened per line so a hard kill can't lose buffered output.
    """

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    with print_lock:
        print(msg, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf8") as f:
                for line in str(msg).splitlines() or [""]:
                    f.write(f"{stamp} {line}\n" if line else "\n")
        except OSError:
            pass  # never let logging kill a run that's otherwise fine


class TaskFailed(RuntimeError):
    """Generation itself failed. `code` decides whether resubmitting is worth it."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code

    @property
    def transient(self):
        # 5xx is a blip on their side and costs no credits; 4xx means the
        # prompt was rejected, so resubmitting it unchanged just wastes time.
        return str(self.code).startswith("5") or self.code in (None, "")


# ===========================
# HTTP WITH RETRIES
# ===========================

def request_json(method, url, **kwargs):
    """One API call, retried on transient failures."""

    kwargs.setdefault("timeout", 60)
    delay = 2

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.request(method, url, headers=HEADERS, **kwargs)

            # Rate limited or server-side blip -> back off and retry
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")

            r.raise_for_status()
            data = r.json()

        except (requests.RequestException, ValueError):
            if attempt == MAX_RETRIES:
                raise
            time.sleep(delay)
            delay *= 2
            continue

        if data.get("code") != 200:
            raise RuntimeError(f"API error: {data}")

        return data["data"]


# ===========================
# CREATE / POLL / DOWNLOAD
# ===========================

def create_task(prompt):
    payload = {
        "model": "gpt-image-2-text-to-image",
        "input": {
            "prompt": prompt,
            "aspect_ratio": ASPECT_RATIO,
            "resolution": RESOLUTION,
        },
    }
    return request_json("POST", CREATE_URL, json=payload)["taskId"]


def wait_for_image(task_id, label):
    deadline = time.monotonic() + TASK_TIMEOUT

    while True:
        data = request_json("GET", STATUS_URL, params={"taskId": task_id})
        state = data.get("state")

        if state == "success":
            result = data.get("resultJson")
            if isinstance(result, str):
                result = json.loads(result)

            urls = result.get("resultUrls") or []
            if not urls:
                raise RuntimeError(f"No resultUrls in response: {result}")

            return urls[0]

        if state == "fail":
            raise TaskFailed(
                data.get("failMsg") or "task failed",
                code=data.get("failCode"),
            )

        if time.monotonic() > deadline:
            raise TimeoutError(f"{label}: still '{state}' after {TASK_TIMEOUT}s")

        time.sleep(POLL_INTERVAL)


def download_image(url, path):
    """Atomic: write to .part, then rename, so partials never look complete."""

    tmp = path + ".part"

    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(8192):
                f.write(chunk)

    if os.path.getsize(tmp) == 0:
        os.remove(tmp)
        raise RuntimeError("downloaded 0 bytes")

    os.replace(tmp, path)


# ===========================
# HELPERS
# ===========================

def safe_name(value):
    return re.sub(r"[^A-Za-z0-9 ._-]", "_", str(value)).strip()


def image_name(item, index):
    """`183 - SPLIT`, falling back to the bare number when type is absent."""

    number = item.get("i", index)
    kind = item.get("type")
    return safe_name(f"{number} - {kind}" if kind else number)


def extension_for(url, fallback=".png"):
    ext = os.path.splitext(urlparse(url).path)[1].lower()
    return ext if ext in (".png", ".jpg", ".jpeg", ".webp") else fallback


def already_done(image_id):
    for ext in (".png", ".jpg", ".jpeg", ".webp"):
        path = os.path.join(OUTPUT_FOLDER, image_id + ext)
        if os.path.exists(path):
            return path
    return None


# ===========================
# WORKER
# ===========================

def process(index, total, item):
    prompt = item.get("image_prompt")
    image_id = image_name(item, index)
    label = f"[{index}/{total}] {image_id}"

    existing = already_done(image_id)
    if existing:
        log(f"{label} exists -> {existing}")
        return "skipped"

    for attempt in range(1, TASK_ATTEMPTS + 1):
        try:
            task_id = create_task(prompt)
            log(f"{label} task {task_id}")

            url = wait_for_image(task_id, label)

            path = os.path.join(OUTPUT_FOLDER, image_id + extension_for(url))
            download_image(url, path)

            log(f"{label} saved -> {path}")
            return "ok"

        except TaskFailed as e:
            if not e.transient or attempt == TASK_ATTEMPTS:
                log(f"{label} ERROR: {e} (failCode {e.code})")
                return "failed"

            log(f"{label} {e} (failCode {e.code}) — resubmitting "
                f"{attempt + 1}/{TASK_ATTEMPTS}")
            time.sleep(POLL_INTERVAL * attempt)

        except Exception as e:
            log(f"{label} ERROR: {e}")
            return "failed"


# ===========================
# REUSED IMAGES
# ===========================

def resolve_source(item, by_number):
    """Follow reuse_of to the beat that actually owns an image.

    Usually one hop, but a chain (215 -> 10 -> 3) still lands on the real one.
    """

    seen = {item.get("i")}
    source = by_number.get(item["reuse_of"])

    while source is not None and source.get("reuse_of") is not None:
        if source["i"] in seen:
            raise RuntimeError(f"reuse_of loops back to {source['i']}")

        seen.add(source["i"])
        source = by_number.get(source["reuse_of"])

    return source


def copy_reuse(index, total, item, by_number):
    """`reuse_of: 10` on beat 215 means: duplicate beat 10's image as 215.

    The source beat's own type decides its filename (10 is an IMAGE even
    though 215 is a SPLIT), so the name comes from the source, not the target.
    """

    image_id = image_name(item, index)
    label = f"[{index}/{total}] {image_id}"

    existing = already_done(image_id)
    if existing:
        log(f"{label} exists -> {existing}")
        return "skipped"

    try:
        source = resolve_source(item, by_number)
    except RuntimeError as e:
        log(f"{label} ERROR: {e}")
        return "failed"

    if source is None:
        log(f"{label} ERROR: reuse_of {item['reuse_of']} is not in {JSON_FILE}")
        return "failed"

    source_path = already_done(image_name(source, source.get("i")))
    if not source_path:
        log(f"{label} ERROR: no image for beat {source['i']} to copy from")
        return "failed"

    path = os.path.join(OUTPUT_FOLDER,
                        image_id + os.path.splitext(source_path)[1])
    tmp = path + ".part"

    shutil.copyfile(source_path, tmp)
    os.replace(tmp, path)

    log(f"{label} copied from {source_path}")
    return "ok"


# ===========================
# MAIN
# ===========================

def main():
    if not API_KEY:
        sys.exit("No API key. Copy .env.example to .env and set KIE_API_KEY.")

    if not os.path.exists(JSON_FILE):
        sys.exit(f"Missing {JSON_FILE} in {os.getcwd()}")

    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    with open(JSON_FILE, "r", encoding="utf8") as f:
        items = json.load(f)

    by_number = {item["i"]: item for item in items if item.get("i") is not None}

    # Three kinds of beat: narration-only (AVATAR, no image_prompt) which we
    # drop, reused beats which are a copy of an earlier image, and the rest
    # which we actually pay to generate. A reused beat carries an image_prompt
    # too, so it has to be filtered out first or we'd generate it twice.
    loaded = len(items)
    reused = [item for item in items if item.get("reuse_of") is not None]
    items = [item for item in items
             if item.get("image_prompt") and item.get("reuse_of") is None]

    total = len(items)
    log(f"\n=== Run started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    log(f"Loaded {loaded} items, {loaded - total - len(reused)} without an "
        f"image_prompt. Generating {total}, copying {len(reused)} reused. "
        f"Concurrency={CONCURRENCY}\n")

    counts = {"ok": 0, "skipped": 0, "failed": 0}

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
        futures = [
            pool.submit(process, i, total, item)
            for i, item in enumerate(items, start=1)
        ]
        for fut in as_completed(futures):
            counts[fut.result()] += 1

    # After generation, never during: the source image has to be on disk
    # before it can be duplicated.
    copied = {"ok": 0, "skipped": 0, "failed": 0}

    if reused:
        log("")
        for index, item in enumerate(reused, start=1):
            copied[copy_reuse(index, len(reused), item, by_number)] += 1

    log(f"\nFinished. {counts['ok']} generated, "
        f"{counts['skipped']} skipped, {counts['failed']} failed. "
        f"Reused: {copied['ok']} copied, {copied['skipped']} skipped, "
        f"{copied['failed']} failed.")

    counts["failed"] += copied["failed"]

    if counts["failed"]:
        log(f"Re-run the script to retry failures — completed images are skipped. "
            f"Details in {LOG_FILE} (grep ERROR).")


if __name__ == "__main__":
    main()
