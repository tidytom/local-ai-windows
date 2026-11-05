"""
local_ai_rules.py - the texts and small helpers that windows_configure.py shares: Hermes's rules
for SOUL.md, the document-ocr skill, and the settings for Hindsight's memory server.
"""

from pathlib import Path

# Settings for Hindsight's memory server (it reads them from its profile file, and from the
# environment Hermes gives it):
HINDSIGHT_SERVER_SETTINGS = {
    # Reading Hindsight's long fact-extraction prompt can take the memory model a few minutes,
    # so the default 120 seconds made every save time out and nothing was kept.
    "HINDSIGHT_API_LLM_TIMEOUT": "900",
    # Ollama answers one request at a time; more at once only makes each one time out.
    "HINDSIGHT_API_LLM_MAX_CONCURRENT": "1",
    # Hindsight's own small search models: keep them off the graphics card, which the main model
    # fills (the first start failed with "CUDA out of memory").
    "HINDSIGHT_API_EMBEDDINGS_LOCAL_FORCE_CPU": "true",
    "HINDSIGHT_API_RERANKER_LOCAL_FORCE_CPU": "true",
}

SOUL_MARKER = "## Skill updates"
# The rules an early version of the setup wrote (under its old file name). Kept word for word so
# they can be found in an existing SOUL.md and replaced with the current rules.
OLD_SOUL_RULES = """\
<!-- Added by hermes_local_setup.py. You can edit or remove these rules. -->
## Skill updates
- When I correct you during a task that uses a skill, update that skill with the correction using your skill tool before you continue.
- After updating a skill, tell me in one line which skill you changed and what you added.
- If a correction should apply to every task, not just one skill, tell me so I can add it to this file.
- Never remove a rule from a skill unless I ask.
"""
SOUL_RULES = """\
<!-- Added by the Local AI setup. You can edit or remove these rules. -->
## Skill updates
- Before you start a task, read "## My rules" below and the skill you will use, and follow them.
- When I correct you during a task that uses a skill, update that skill with the correction using your skill tool before you continue.
- After updating a skill, tell me in one line which skill you changed and what you added.
- If a correction should apply to every task, not just one skill, add it yourself as a new line under "## My rules" in this file (~/.hermes/SOUL.md), then tell me in one line what you added.
- Never remove or weaken a rule, in a skill or in this file, unless I ask. If a new correction conflicts with an existing rule, ask me which one to keep.
- Every time you add or change a rule, add one dated line under "## Rule changes": what changed, and what went wrong before that made the change necessary.

## My rules
<!-- Rules for every task. Hermes adds them here when I correct it; I can edit them too. -->

## Rule changes
<!-- One dated line per change: what changed and why. -->
"""

# {DOCS_TOOL} and {FIELDS_FILE} are filled in by windows_configure.py.
SKILL_TEXT = """---
name: document-ocr
description: Read photos, scans, screenshots and PDFs with the local OCR and vision models, extract and check the values, and find documents processed before. Use when the user asks to OCR, scan, read, extract data from, or search documents or images.
---

# Document OCR

Everything runs on this computer. The document tool is `{DOCS_TOOL}`.

## Process documents

1. Run `{DOCS_TOOL} process "<file or folder>"`.
   For more than about 20 files, add `--background`, then check progress every few minutes with `{DOCS_TOOL} status` instead of waiting.
   - The tool turns sideways photos upright, reads every page with glm-ocr and qwen3-vl, extracts the values with the data-processing model, checks each value against the text of its page, and has the reviewer look at doubtful values in the page image.
   - If it stops, run the same command again: it continues where it stopped. Files already done are skipped unless they changed.
2. It prints where the results are: a `hermes_docs` folder next to the originals, with one Markdown and one JSON file per document.
3. Read the JSON files. Every value has a page number, an exact quote and a status:
   - verified, verified_by_review: found on the page.
   - corrected_by_review: the reviewer fixed it after looking at the page image; the original value is kept.
   - disputed: the text and the reviewer disagree. Tell the user to check it.
   - unverified, not_found_on_page: not confirmed. Don't treat these as facts.
4. Report results with the file name and page for every value. List missing values, discrepancies and anything not confirmed. Never fill in a missing value yourself.

## Find documents processed before

- `{DOCS_TOOL} search "words"` lists matching documents with file and page.
- `{DOCS_TOOL} show "<original file>" --page N` shows one page's text and the path of its image. Use your vision tool on that image when you need to check a value yourself.

## Memory

- Document text lives in the hermes_docs files and the search index, not in memory.
- When you report important facts from documents, name the file and page, so the memory keeps the source.

## Extra fields

The fields to look for, by document type, are in `{FIELDS_FILE}`. When the user asks to always extract something new, add it there (keep the JSON valid) and tell the user what you added.
"""


def write_file(path, text, mode=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mode:
        path.chmod(mode)


def update_env_file(path, values):
    """Set KEY=value lines in a .env file, keeping the other lines."""
    lines = path.read_text().splitlines() if path.exists() else []
    kept = [line for line in lines if line.split("=", 1)[0].strip() not in values]
    kept += [f"{key}={value}" for key, value in values.items()]
    write_file(path, "\n".join(kept) + "\n", mode=0o600)


def section(parent, name):
    value = parent.get(name)
    if not isinstance(value, dict):
        value = {}
        parent[name] = value
    return value


def set_profile_settings(path, values):
    """Set KEY=value lines in Hindsight's profile file, replacing a commented-out example of the
    same key where there is one and keeping everything else."""
    lines = path.read_text().splitlines() if path.exists() else []
    done = set()
    for index, line in enumerate(lines):
        key = line.lstrip("# ").split("=", 1)[0].strip()
        if key in values and key not in done and "=" in line:
            lines[index] = f"{key}={values[key]}"
            done.add(key)
    lines += [f"{key}={value}" for key, value in values.items() if key not in done]
    write_file(path, "\n".join(lines) + "\n", mode=0o600)
