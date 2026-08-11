"""Runs the whole pipeline, one step at a time, in order.

    extract_avatar_prompts.py  ->  narration for the avatar/split beats
    generate_images.py         ->  the stills
    generate_motion_prompts.py ->  motion prompts for the b-roll stills
    generate_videos.py         ->  the clips

Each step's output is the next step's input, so they run in sequence, not in
parallel. A step that fails outright stops the run — there's no point spending
money on a stage whose input never arrived.

Every step is resumable, so re-running this after a failure picks up where it
left off rather than starting over.

    python run_all.py                    # the lot
    python run_all.py --dry-run          # plan only, spends nothing
    python run_all.py --limit 2          # cap the video step at 2 clips
    python run_all.py --from motion      # skip ahead
    python run_all.py --only images      # just one step
"""

import argparse
import os
import subprocess
import sys
import time

from dotenv import load_dotenv

# ===========================
# CONFIG
# ===========================

load_dotenv()

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = "run_all.log"

# name, script, what it does, the key it needs (None = free)
STEPS = [
    ("avatar", "extract_avatar_prompts.py",
     "Extract AVATAR + SPLIT narration", None),
    ("images", "generate_images.py",
     "Generate the stills (KIE)", "KIE_API_KEY"),
    ("motion", "generate_motion_prompts.py",
     "Write the motion prompts (Gemini)", "GEMINI_API_KEY"),
    ("videos", "generate_videos.py",
     "Generate the clips (Replicate)", "REPLICATE_API_TOKEN"),
]

NAMES = [name for name, _, _, _ in STEPS]

# Flags this runner forwards, and the steps that understand them.
FORWARDED = {
    "--dry-run": ("videos",),
    "--limit": ("videos",),
}


def log(msg):
    """Mirrors the runner's own banners to a file; each step keeps its own log."""

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")

    print(msg, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf8") as f:
            for line in str(msg).splitlines() or [""]:
                f.write(f"{stamp} {line}\n" if line else "\n")
    except OSError:
        pass


def elapsed(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


# ===========================
# RUN ONE STEP
# ===========================

def run_step(name, script, description, args):
    """One script, output streaming straight through to the terminal."""

    command = [sys.executable, script]

    if args.dry_run and name in FORWARDED["--dry-run"]:
        command.append("--dry-run")

    if args.limit and name in FORWARDED["--limit"]:
        command += ["--limit", str(args.limit)]

    log(f"\n{'=' * 70}")
    log(f"  {name.upper()}  —  {description}")
    log(f"  {' '.join(os.path.basename(c) for c in command)}")
    log(f"{'=' * 70}\n")

    started = time.monotonic()

    # No capture: the sub-scripts already print live progress, and each one
    # writes its own log file.
    result = subprocess.run(command, cwd=HERE)

    took = time.monotonic() - started

    if result.returncode != 0:
        log(f"\n{name} FAILED (exit {result.returncode}) after {elapsed(took)}")

    return result.returncode, took


# ===========================
# MAIN
# ===========================

def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--from", dest="start", choices=NAMES,
                        help="start at this step instead of the first")
    parser.add_argument("--only", choices=NAMES,
                        help="run just this step")
    parser.add_argument("--skip", action="append", choices=NAMES, default=[],
                        help="skip a step (repeatable)")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="cap the video step at N clips")
    parser.add_argument("--dry-run", action="store_true",
                        help="plan the video step without submitting anything")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="don't ask before starting a run that costs money")
    args = parser.parse_args()

    steps = STEPS

    if args.only:
        steps = [s for s in steps if s[0] == args.only]
    elif args.start:
        steps = steps[NAMES.index(args.start):]

    steps = [s for s in steps if s[0] not in args.skip]

    if not steps:
        sys.exit("Nothing to run — everything was skipped.")

    # Check every key up front. Finding out the Replicate token is missing
    # after an hour of image generation is a waste of an hour.
    missing = sorted({
        key for _, _, _, key in steps
        if key and not os.environ.get(key)
    })
    if missing and not args.dry_run:
        sys.exit(f"Missing from .env: {', '.join(missing)}")

    paid = [name for name, _, _, key in steps if key] if not args.dry_run else []

    log(f"\n=== Pipeline started {time.strftime('%Y-%m-%d %H:%M:%S')} ===")
    for position, (name, script, description, key) in enumerate(steps, start=1):
        cost = "costs money" if key and not args.dry_run else "free"
        log(f"  {position}. {name:7} {script:28} {description} ({cost})")

    if paid and not args.yes:
        if not sys.stdin.isatty():
            sys.exit("\nThis run spends money. Re-run with --yes to confirm.")

        log(f"\nSteps that spend money: {', '.join(paid)}")
        if input("Continue? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Cancelled.")

    results = []
    failed = None

    for name, script, description, _ in steps:
        try:
            code, took = run_step(name, script, description, args)
        except KeyboardInterrupt:
            log(f"\n\nInterrupted during {name}. "
                f"Re-run to resume — finished work is skipped.")
            sys.exit(130)

        results.append((name, code, took))

        if code != 0:
            failed = name
            break

    log(f"\n{'=' * 70}")
    log("  SUMMARY")
    log(f"{'=' * 70}")

    for name, code, took in results:
        log(f"  {'ok  ' if code == 0 else 'FAIL'}  {name:8} {elapsed(took)}")

    total = sum(took for _, _, took in results)
    log(f"\nTotal {elapsed(total)}.")

    if failed:
        remaining = NAMES[NAMES.index(failed):]
        log(f"Stopped at '{failed}'. Fix it, then: "
            f"python run_all.py --from {failed}")
        log(f"Not run: {', '.join(remaining[1:]) or 'nothing'}")
        sys.exit(1)

    log("All steps finished. Check each step's own log for per-item failures — "
        "a step can finish cleanly while individual beats fail.")


if __name__ == "__main__":
    main()
