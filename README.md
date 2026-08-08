# kie-image-gen

Turns a beat sheet (`prompts.json`) into the visual assets for a video:
still images, avatar narration lines, motion prompts, and 6-second b-roll clips.

---

## Commands

Every command runs from the project root. Use the venv's interpreter — the
dependencies are not installed system-wide.

### Setup (once)

```bash
cd /Users/marvin/kie-image-gen
python3 -m venv .venv                          # only if .venv is missing
.venv/bin/pip install -r requirements.txt
cp .env.example .env                           # then fill in your keys
```

### 1. Generate the images

```bash
.venv/bin/python generate_images.py
```

### 2. Extract the avatar narration

```bash
.venv/bin/python extract_avatar_prompts.py
```

### 3. Write the motion prompts (b-roll only)

```bash
.venv/bin/python generate_motion_prompts.py

.venv/bin/python generate_motion_prompts.py --models    # list usable model ids
.venv/bin/python generate_motion_prompts.py --redo      # ignore existing results
```

### 4. Generate the video clips

```bash
.venv/bin/python generate_videos.py --dry-run           # show the plan, bill nothing
.venv/bin/python generate_videos.py --limit 2           # try 2 clips first
.venv/bin/python generate_videos.py                     # all of them

.venv/bin/python generate_videos.py --motion dolly_in   # force one camera move
.venv/bin/python generate_videos.py --seed 42           # repeatable camera moves
```

### Prefer plain `python`?

```bash
source .venv/bin/activate
python generate_images.py
deactivate
```

---

## What you need

| Key | Used by | Where to get it |
| --- | --- | --- |
| `KIE_API_KEY` | `generate_images.py` | https://kie.ai |
| `GEMINI_API_KEY` | `generate_motion_prompts.py` | https://aistudio.google.com/apikey |
| `GEMINI_MODEL` | `generate_motion_prompts.py` | optional, defaults to `gemini-3.1-pro-preview` |
| `REPLICATE_API_TOKEN` | `generate_videos.py` | https://replicate.com/account/api-tokens |

All four live in `.env`, which is gitignored. `.env.example` is the committed
template — keep real keys out of it.

---

## The pipeline, step by step

```
prompts.json
     │
     ├── generate_images.py ──────────► images/          (IMAGE, SPLIT, BROLL beats)
     │
     ├── extract_avatar_prompts.py ───► avatar-prompt.json   (AVATAR + SPLIT beats)
     │
     └── BROLL beats
              │
              ▼
     generate_motion_prompts.py ──────► motion-prompt.json
              │   (image + narration → Gemini vision)
              ▼
        generate_videos.py ───────────► videos/ + video-manifest.json
                  (image + motion prompt → LTX-2.3-fast)
```

### Step 0 — the beat sheet

`prompts.json` is a list of beats. Each one looks like this:

```json
{
  "i": 215,
  "t_start": 1261.9,
  "t_end": 1267.7,
  "type": "SPLIT",
  "format": "HANDS",
  "narration": "the thing your grandmother made that you can still taste...",
  "image_prompt": "Elderly, flour-dusted hands working a lump of rough dough...",
  "reuse_of": 10
}
```

| Field | Meaning |
| --- | --- |
| `i` | Beat number. Drives every filename. |
| `t_start` / `t_end` | Position in the edit, in seconds. |
| `type` | `IMAGE`, `SPLIT`, `BROLL`, or `AVATAR`. Decides which scripts touch it. |
| `format` | `WIDE`, `DETAIL`, `HANDS`, `OBJECT`, `PAPER`. Descriptive only. |
| `narration` | The spoken line for this beat. |
| `image_prompt` | What to generate. `null` on AVATAR beats. |
| `reuse_of` | Another beat's `i` — this beat shows that beat's image again. |

The four types:

- **IMAGE / SPLIT** — a still. Generated, then used as-is.
- **BROLL** — a still that becomes a moving clip. Goes through all of steps 1, 3 and 4.
- **AVATAR** — narration only, no picture. Skipped by the image generator.

### Step 1 — `generate_images.py`

Reads `prompts.json`, writes PNGs to `images/`.

Sends each `image_prompt` to KIE's `gpt-image-2-text-to-image` at **2K, 16:9**,
polls until the task finishes, then downloads the result. Three beats run at a
time.

Files are named **`<i> - <TYPE>.png`** — `1 - BROLL.png`, `215 - SPLIT.png`.
Every later step finds images by that name, so don't rename them.

**Reused beats.** A beat with `reuse_of: 10` is not generated — its image is
copied from beat 10 after the main pass finishes. The filename comes from the
*source* beat's type, so `215 - SPLIT.png` is copied from `10 - IMAGE.png`.
Chains (`300 → 215 → 10`) resolve to the beat that actually owns the image, and
a loop is reported instead of hanging.

**What it skips.** AVATAR beats (no `image_prompt`), and any beat whose image
file already exists.

Knobs at the top of the file: `ASPECT_RATIO`, `RESOLUTION`, `CONCURRENCY`,
`TASK_TIMEOUT`, `MAX_RETRIES`, `TASK_ATTEMPTS`.

**Retries.** HTTP failures and 5xx responses back off and retry. A server-side
generation failure is resubmitted up to `TASK_ATTEMPTS` times; a 4xx failure
(a rejected prompt) is not, because the same prompt would be rejected again.

Log: `generate_images.log`.

### Step 2 — `extract_avatar_prompts.py`

Reads `prompts.json`, writes `avatar-prompt.json`.

Pulls every **AVATAR** and **SPLIT** beat's narration into one file with its
timings:

```json
[
  {
    "i": 0,
    "t_start": 0,
    "t_end": 5,
    "type": "AVATAR",
    "narration": "Winter, nineteen eighteen. The coldest stretch anybody..."
  }
]
```

Timings are kept so the lines can be lined back up with the edit, and `type` is
kept because the two kinds are read differently downstream — an AVATAR beat is
narration only, while a SPLIT beat also has a picture in `images/`. Beats with
empty narration are reported and skipped. Order follows `prompts.json`.

Which types get pulled is the `TARGET_TYPES` tuple at the top of the file — add
`"IMAGE"` or `"BROLL"` there to widen it.

No API key, no cost, instant. Always rewrites the whole file, so re-run it any
time `prompts.json` changes.

### Step 3 — `generate_motion_prompts.py`

Reads `prompts.json` + `images/` + `engineer2_motion_vision.txt`,
writes `motion-prompt.json`.

For each **BROLL** beat, sends Gemini the generated image plus the narration
line, with `engineer2_motion_vision.txt` as the system prompt. Gemini looks at
the picture and returns a short motion prompt describing how it should come to
life — subject motion, ambient motion, and amateur handheld camera drift.

Output record:

```json
{
  "i": 1,
  "t_start": 3.5,
  "t_end": 9.7,
  "format": "WIDE",
  "narration": "snow up to the windowsills...",
  "image": "images/1 - BROLL.png",
  "motion_prompt": "The distant bare tree branches sway faintly in a cold wind..."
}
```

**Resumable.** Beats already in `motion-prompt.json` are skipped, and results
are written after every success — an interrupted run keeps what it paid for.
Use `--redo` to regenerate everything.

**Waiting on images.** A BROLL beat whose image doesn't exist yet is reported
and skipped, not failed. Run step 1, then re-run this.

**Model.** Set `GEMINI_MODEL` in `.env`. `--models` lists what your key can
actually use. Pro-class models need billing enabled (see Costs below); if the
key has no quota for the chosen model, the run stops after the first failure
rather than repeating it for every beat.

Knobs: `CONCURRENCY`, `MAX_RETRIES`, `RETRY_CAP`, `TEMPERATURE`.

Log: `motion_prompts.log`.

### Step 4 — `generate_videos.py`

Reads `motion-prompt.json` + `images/`, writes `videos/` + `video-manifest.json`.

Runs each beat through **`lightricks/ltx-2.3-fast`** on Replicate: the image as
the starting frame, the motion prompt as the prompt.

Fixed settings:

| Setting | Value |
| --- | --- |
| `resolution` | `1080p` |
| `duration` | `6` seconds |
| `fps` | `25` |
| `aspect_ratio` | `16:9` (matches the generated images) |
| `generate_audio` | `false` (the model defaults this to **true**) |
| `image` | starting frame only — no `last_frame_image` |

**Camera motion** is chosen at random per clip from the `CAMERA_MOTIONS` list
at the top of the file — currently `dolly_left`, `dolly_right`, `dolly_in`,
`dolly_out`. The model also accepts `jib_up`, `jib_down`, `static`,
`focus_shift` and `none` if you want them back. Never the same
move on two consecutive beats, since neighbouring identical moves read as a
mistake in the edit. Moves are assigned in beat order before anything is
submitted. Every choice is recorded in `video-manifest.json`; `--seed N` makes
a run repeatable.

Clips are named to match their image: `images/12 - BROLL.png` → `videos/12 - BROLL.mp4`.

**Start small.** `--dry-run` prints the exact plan without calling anything.
`--limit 2` does two clips. Replicate bills per run, so check quality before
committing to the full set.

**Retries.** Replicate-side errors back off and retry. A `ModelError` — the
model ran and refused the input — is not retried, because the same input would
be refused again.

Log: `generate_videos.log`.

---

## Re-running

Every script is safe to re-run and resumes where it left off:

| Script | Skips work when |
| --- | --- |
| `generate_images.py` | `images/<i> - <TYPE>.png` exists |
| `extract_avatar_prompts.py` | never — always rewrites, it's free |
| `generate_motion_prompts.py` | the beat is already in `motion-prompt.json` |
| `generate_videos.py` | `videos/<i> - <TYPE>.mp4` exists |

To force a redo, delete the file in question (or pass `--redo` in step 3).

Downloads and copies are written to a `.part` file and renamed only once
complete, so an interrupted run never leaves a truncated file that later looks
finished.

---

## Logs

Each script mirrors its console output to a timestamped `.log` beside it, so a
closed terminal doesn't lose the record. Logs append; runs are separated by a
`=== Run started ... ===` banner.

```bash
grep ERROR generate_images.log
grep ERROR motion_prompts.log
grep ERROR generate_videos.log
```

`*.log` is gitignored.

---

## Costs and quotas

**KIE (step 1)** — billed per image. A 4xx generation failure means the prompt
was rejected; it isn't retried, so it costs nothing further.

**Gemini (step 3)** — roughly **2,000 tokens per beat** (~1,600 in, ~450 out
including thinking tokens), measured on a real 2K image. The image dominates;
the system prompt is only ~500 tokens.

Pro-class models (`gemini-3.1-pro-preview`, `gemini-3-pro-preview`) have a free
tier quota of **zero** — a 429 saying `limit: 0` means the key has no allowance
at all, not that you used it up. The fix is enabling billing on the Google Cloud
project the key belongs to. Flash models work on the free tier. Requests that
fail with 429 are rejected before inference and cost nothing.

**Replicate (step 4)** — billed per run. Always `--dry-run` and `--limit` first.

---

## Troubleshooting

**`No API key` / `No API token`** — the key is missing from `.env`. Note that
`.env.example` is only a template; nothing reads it.

**`Missing prompts.json in <dir>`** — paths are relative, so run from the
project root.

**`no image yet — run generate_images.py first`** (step 3) — that BROLL beat
has no image. Run step 1 and re-run step 3.

**`has no quota on this API key`** (step 3) — free tier against a Pro model.
Enable billing, or set `GEMINI_MODEL` to a flash model.

**`Missing motion-prompt.json`** (step 4) — step 3 hasn't produced anything yet.

**`ModuleNotFoundError`** — you used system `python` instead of
`.venv/bin/python`.

---

## Files

| Path | What it is |
| --- | --- |
| `prompts.json` | The beat sheet. Input to everything. |
| `engineer2_motion_vision.txt` | System prompt for the Gemini vision step. |
| `images/` | Generated stills, `<i> - <TYPE>.png`. Gitignored. |
| `avatar-prompt.json` | AVATAR + SPLIT narration lines with timings. |
| `motion-prompt.json` | Motion prompts for BROLL beats. |
| `videos/` | Generated clips, `<i> - <TYPE>.mp4`. Gitignored. |
| `video-manifest.json` | Which camera move each clip got. |
| `*.log` | Run logs. Gitignored. |
| `.env` | Your keys. Gitignored. |
