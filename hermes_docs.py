#!/usr/bin/python3
"""
hermes-docs: read photos, scans, screenshots and PDFs with local AI models,
check every value against its page, and keep a searchable index.

  hermes-docs process PATH [--profile quality|balanced] [--background] [--force] [--no-review]
  hermes-docs status [--json]
  hermes-docs search "words" [--limit N] [--json]
  hermes-docs show FILE [--page N] [--json]
  hermes-docs forget PATH          remove documents from the index (files are kept)
  hermes-docs selftest [--json] [--keep]
  hermes-docs benchmark --processors MODEL[,MODEL...] [--json]
  hermes-docs samples FOLDER       make three test images with known values

Everything runs on this computer through Ollama. Original files are never
changed: results go into a "hermes_docs" folder next to them.

How a document is processed (one stage at a time for all documents, so each
model is loaded only once on a 12 GB GPU):
  1. Prepare: turn photos upright (camera setting, then a text-direction
     check with Tesseract), scale very large photos, split PDFs into pages.
  2. Read: glm-ocr reads the text. qwen3-vl describes the image, reads it a
     second time and spots tables (every page in the quality profile, only
     unclear pages in the balanced profile).
  3. Extract: the data-processing model returns JSON. Every value must come
     with its page number and an exact quote from that page.
  4. Check: code checks that each quote is on its page and contains the value,
     and that line items add up to the total.
  5. Review: the reviewer model looks at the page image for doubtful values.
  6. Save: Markdown and JSON next to the originals, plus the search index.
"""

import argparse
import base64
import collections
import datetime
import difflib
import hashlib
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path.home()
WINDOWS = os.name == "nt"


def hermes_home():
    """Hermes keeps its files in ~/.hermes on Linux and in %LOCALAPPDATA%\\hermes on Windows."""
    if os.environ.get("HERMES_HOME"):
        return Path(os.environ["HERMES_HOME"])
    if WINDOWS and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "hermes"
    return HOME / ".hermes"


def settings_file():
    """local_ai_docs.json in Hermes's folder; installs made before the rename keep locallinux.json."""
    new, old = hermes_home() / "local_ai_docs.json", hermes_home() / "locallinux.json"
    return old if old.exists() and not new.exists() else new


SETTINGS_FILE = Path(os.environ.get("HERMES_DOCS_SETTINGS", settings_file()))
DATA_DIR = Path(os.environ.get("HERMES_DOCS_HOME", hermes_home() / "docs"))
OUTPUT_DIR_NAME = "hermes_docs"
SKIP_DIRS = {OUTPUT_DIR_NAME, "ocr_output", "__pycache__"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp", ".heic", ".heif"}
SUPPORTED = IMAGE_TYPES | {".pdf"}

MAX_SIDE = 2560        # longest side of the prepared page images (kept for review)
VISION_SIDE = 2048     # longest side of images sent to the vision model
REVIEW_SIDE = 1600     # longest side of images sent to the reviewer
CHUNK_CHARS = 60000    # page text per extraction request (about 15K tokens)
KEEP_ALIVE = "30m"
TIMEOUT = 1800
# Room for one large photo plus its text. Ollama's default (often 4K) is too small.
# The setup script makes model copies with these same sizes, so nothing reloads.
OCR_CTX = 16384
VISION_CTX = 32768
AGREEMENT_LIMIT = 0.80  # below this, the two readings of a page count as different

KINDS = ["receipt", "invoice", "form", "printed_page", "handwritten_note", "screenshot",
         "whiteboard", "chart_or_diagram", "photo", "other"]
GOOD = {"verified", "verified_by_review", "corrected_by_review"}
DOUBTFUL_FLAGS = ("readings_differ", "numbers_differ", "text_from_vision", "ocr_unclear")

DEFAULT_FIELDS = {
    "receipt": ["store name", "date", "time", "subtotal", "tax", "total", "payment method"],
    "invoice": ["invoice number", "invoice date", "due date", "seller", "buyer", "subtotal",
                "tax", "total"],
    "form": ["form title", "every filled-in field with its label", "date", "signature present"],
    "handwritten_note": ["date", "names", "action items"],
    "printed_page": ["title", "date", "names", "key numbers"],
    "screenshot": ["app or website", "key values shown"],
    "whiteboard": ["headings", "action items"],
    "chart_or_diagram": ["title", "axis labels", "key values"],
    "photo": [],
    "other": [],
}

DEFAULT_SETTINGS = {
    "ollama": "http://127.0.0.1:11434" if WINDOWS else "http://localhost:11434",
    "profile": "quality",
    "models": {"ocr": "glm-ocr:16k", "vision": "qwen3-vl:8b-32k",
               "processor": "gemma4:26b-128k", "reviewer": "gemma4:26b-128k"} if WINDOWS else
              {"ocr": "glm-ocr", "vision": "qwen3-vl:8b",
               "processor": "nemotron-helper", "reviewer": "gemma4-hermes"},
}

LOG_HANDLE = None
# Progress of the current "process" job, saved in job.json so the Local AI app can show it.
JOB = {}
JOB_SAVED = [0.0]
STAGE_TITLES = {
    "prepare": "Preparing photos and pages",
    "vision": "Looking at each page",
    "ocr": "Reading the text",
    "combine": "Comparing the two readings",
    "extract": "Extracting the values",
    "check": "Checking every value against its page",
    "review": "Reviewing doubtful values",
    "write": "Saving the results and the search index",
}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def log(message=""):
    print(message, flush=True)
    if LOG_HANDLE:
        LOG_HANDLE.write(message + "\n")
        LOG_HANDLE.flush()


def job_file():
    return DATA_DIR / "job.json"


def job_save(force=False, **fields):
    """Update the job's progress (written at most twice a second unless forced)."""
    if not JOB:
        return
    JOB.update(fields)
    if not force and time.time() - JOB_SAVED[0] < 0.5:
        return
    JOB_SAVED[0] = JOB["updated"] = time.time()
    try:
        temp = job_file().with_suffix(".tmp")
        temp.write_text(json.dumps(JOB))
        temp.replace(job_file())
    except OSError:
        pass


def job_stage(key, total, model=None):
    """A new pipeline stage starts: total is how many pages or documents it handles."""
    if JOB:
        title = STAGE_TITLES[key] + (f" ({model})" if model else "")
        job_save(force=True, stage=key, stage_number=JOB.get("stage_number", 0) + 1,
                 stage_title=title, done=0, total=total, current="")


def job_step(done, current):
    job_save(done=done, current=current)


def read_job():
    try:
        return json.loads(job_file().read_text())
    except (OSError, ValueError):
        return {}


def now_text():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def safe_name(text, limit=120):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:limit] or "file"


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def full_name(name):
    return (name if ":" in name.split("/")[-1] else name + ":latest").lower()


def load_settings():
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        saved = json.loads(SETTINGS_FILE.read_text())
    except (OSError, ValueError):
        saved = {}
    docs = saved.get("docs", saved) if isinstance(saved, dict) else {}
    for key in ("ollama", "profile"):
        if docs.get(key):
            settings[key] = docs[key]
    settings["models"].update({k: v for k, v in (docs.get("models") or {}).items() if v})
    return settings


def fields_config():
    path = DATA_DIR / "fields.json"
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(DEFAULT_FIELDS, indent=2) + "\n")
    return DEFAULT_FIELDS


INSTALL_HINTS = {   # how to install each helper program, on Linux and on Windows
    "poppler-utils": ("sudo apt install poppler-utils", "winget install oschwartz10612.Poppler"),
    "tesseract-ocr": ("sudo apt install tesseract-ocr", "winget install UB-Mannheim.TesseractOCR"),
    "python3-pil": ("sudo apt install python3-pil", "py -m pip install pillow"),
}


def install_hint(package):
    linux, windows = INSTALL_HINTS.get(package, (f"sudo apt install {package}", package))
    return windows if WINDOWS else linux


def find_tool(command):
    """Full path of a helper program, or None. On Windows also looks where the usual installers
    put them, since they don't always add themselves to PATH."""
    found = shutil.which(command)
    if found or not WINDOWS:
        return found
    roots = [Path(os.environ.get(v, "")) for v in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")]
    patterns = [f"Tesseract-OCR/{command}.exe", f"poppler*/Library/bin/{command}.exe",
                f"poppler*/bin/{command}.exe", f"Microsoft/WinGet/Links/{command}.exe",
                f"Microsoft/WinGet/Packages/*Poppler*/*/Library/bin/{command}.exe",
                f"Microsoft/WinGet/Packages/*Poppler*/*/bin/{command}.exe"]
    for root in roots:
        if not str(root) or not root.exists():
            continue
        for pattern in patterns:
            for match in sorted(root.glob(pattern), reverse=True):
                return str(match)
    return None


def pil():
    try:
        from PIL import Image, ImageOps
    except ImportError:
        sys.exit("hermes-docs needs Pillow. Install it with: " + install_hint("python3-pil"))
    try:   # lets Pillow open iPhone (HEIC) photos, where pillow-heif is installed
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    return Image, ImageOps


def need(command, package):
    found = find_tool(command)
    if not found:
        raise RuntimeError(f"{command} is missing. Install it with: {install_hint(package)}")
    return found


# --------------------------------------------------------------------------
# Talking to Ollama
# --------------------------------------------------------------------------
class Ollama:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.shown = {}

    def request(self, path, body=None, timeout=TIMEOUT):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            raise RuntimeError(str(detail).strip()[:300]) from None
        except (urllib.error.URLError, OSError) as error:
            raise RuntimeError(f"can't reach Ollama at {self.base} ({error})") from None

    def installed(self):
        return {m["name"].lower() for m in self.request("/api/tags", timeout=30).get("models", [])}

    def show(self, model):
        if model not in self.shown:
            try:
                self.shown[model] = self.request("/api/show", {"model": model}, timeout=60)
            except RuntimeError:
                self.shown[model] = {}
        return self.shown[model]

    def can_see(self, model):
        capabilities = self.show(model).get("capabilities")
        return True if capabilities is None else "vision" in capabilities

    def chat(self, model, prompt, images=(), system=None, schema=None, num_ctx=None):
        messages = [{"role": "system", "content": system}] if system else []
        message = {"role": "user", "content": prompt}
        if images:
            message["images"] = list(images)
        messages.append(message)
        options = {"temperature": 0}
        if num_ctx:
            options["num_ctx"] = num_ctx
        body = {"model": model, "messages": messages, "stream": False,
                "keep_alive": KEEP_ALIVE, "options": options}
        if schema:
            body["format"] = schema
        answer = self.request("/api/chat", body)
        return ((answer.get("message") or {}).get("content") or "").strip()

    def chat_json(self, model, prompt, schema, images=(), system=None, num_ctx=None):
        text = self.chat(model, prompt, images, system, schema, num_ctx)
        try:
            return json.loads(text)
        except ValueError:
            text = self.chat(model, prompt + "\n\nReturn only valid JSON that matches the schema.",
                             images, system, schema, num_ctx)
            return json.loads(text)


def encode_image(path, max_side):
    Image, _ = pil()
    with Image.open(path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


# --------------------------------------------------------------------------
# Preparing pages: upright, scaled, one PNG per page
# --------------------------------------------------------------------------
def tesseract_rotation(path):
    """(degrees to turn clockwise, confidence) from Tesseract's direction check, or None."""
    tesseract = find_tool("tesseract")
    if not tesseract:
        return None
    try:
        out = subprocess.run([tesseract, str(path), "-", "--psm", "0"],
                             capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return None
    text = out.stdout + out.stderr
    rotate = re.search(r"Rotate:\s*(\d+)", text)
    confidence = re.search(r"Orientation confidence:\s*([\d.]+)", text)
    if not rotate or not confidence:
        return None
    return int(rotate.group(1)) % 360, float(confidence.group(1))


def flatten(image):
    Image, _ = pil()
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        image = image.convert("RGBA")
        background = Image.new("RGB", image.size, "white")
        background.paste(image, mask=image.split()[-1])
        return background
    return image.convert("RGB")


def save_page_image(image, target):
    """Turn a page upright, scale it and save it as PNG. Returns the degrees turned."""
    _, ImageOps = pil()
    image = flatten(ImageOps.exif_transpose(image))
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    image.save(target, "PNG")
    found = tesseract_rotation(target)
    if found and found[0] in (90, 180, 270) and found[1] >= 2.0:
        turned = image.rotate(-found[0], expand=True, fillcolor="white")
        turned.save(target, "PNG")
        check = tesseract_rotation(target)
        if check and check[0] == 0:
            return found[0]
        image.save(target, "PNG")  # the second check disagreed: keep it as it was
    return 0


def heic_in_pillow():
    pil()
    try:
        import pillow_heif  # noqa: F401
        return True
    except ImportError:
        return False


def pdf_page_count(path):
    pdfinfo = need("pdfinfo", "poppler-utils")
    out = subprocess.run([pdfinfo, str(path)], capture_output=True, text=True, timeout=120)
    match = re.search(r"^Pages:\s+(\d+)", out.stdout, re.MULTILINE)
    if not match:
        raise RuntimeError("couldn't read the number of pages")
    return int(match.group(1))


def prepare_document(path, pages_dir, key):
    """Make one upright PNG per page. Returns a list of page dicts."""
    Image, _ = pil()
    pages = []
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pdftoppm = need("pdftoppm", "poppler-utils")
        pdftotext = need("pdftotext", "poppler-utils")
        for number in range(1, pdf_page_count(path) + 1):
            text = subprocess.run([pdftotext, "-f", str(number), "-l", str(number), "-layout",
                                   str(path), "-"], capture_output=True, text=True,
                                  timeout=300).stdout
            target = pages_dir / f"{key}-p{number}.png"
            subprocess.run([pdftoppm, "-r", "200", "-f", str(number), "-l", str(number), "-png",
                            "-singlefile", str(path), str(target.with_suffix(""))],
                           check=True, capture_output=True, timeout=600)
            has_text = len(text.strip()) >= 80
            rotation = 0
            if not has_text:
                with Image.open(target) as image:
                    rotation = save_page_image(image.copy(), target)
            pages.append({"page": number, "image": str(target), "rotation": rotation,
                          "source": "pdf-text" if has_text else "image",
                          "pdf_text": text if has_text else None})
        return pages

    temp = None
    source = path
    if suffix in (".heic", ".heif") and not heic_in_pillow():
        tool = shutil.which("heif-dec") or shutil.which("heif-convert")
        if not tool:
            raise RuntimeError("HEIC photos need libheif-examples. Install it with: " +
                               ("py -m pip install pillow-heif" if WINDOWS
                                else "sudo apt install libheif-examples"))
        temp = tempfile.TemporaryDirectory()
        source = Path(temp.name) / "converted.png"
        subprocess.run([tool, str(path), str(source)], check=True, capture_output=True, timeout=300)
    try:
        with Image.open(source) as image:
            for index in range(getattr(image, "n_frames", 1)):
                image.seek(index)
                target = pages_dir / f"{key}-p{index + 1}.png"
                rotation = save_page_image(image.copy(), target)
                pages.append({"page": index + 1, "image": str(target), "rotation": rotation,
                              "source": "image", "pdf_text": None})
    finally:
        if temp:
            temp.cleanup()
    return pages


# --------------------------------------------------------------------------
# Text checks
# --------------------------------------------------------------------------
def normalize(text):
    text = (text or "").casefold()
    for old, new in (("\u2019", "'"), ("\u2018", "'"), ("\u201c", '"'), ("\u201d", '"')):
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip()


def words(text):
    return re.findall(r"\w+", normalize(text))


def digits(text):
    return re.sub(r"\D", "", text or "")


def word_agreement(a, b):
    """How much two readings of the same page agree (0 to 1), ignoring word order."""
    count_a, count_b = collections.Counter(words(a)), collections.Counter(words(b))
    total = sum(count_a.values()) + sum(count_b.values())
    return 1.0 if not total else 2 * sum((count_a & count_b).values()) / total


def numbers_in(text):
    """The numbers in a text, as {digits only: as written}."""
    found = re.findall(r"\d[\d.,:/-]*\d|\d", text or "")
    return {digits(n): n for n in found if len(digits(n)) >= 2}


FLAG_TEXT = {
    "readings_differ": "the OCR and vision readings of this page differ a lot",
    "numbers_differ": "the OCR and vision readings have different numbers",
    "text_from_vision": "the OCR model found almost no text, so the vision model's reading was used",
    "ocr_unclear": "the OCR reading looks unreliable",
    "unreadable_parts": "some parts couldn't be read",
}


def describe_flag(flag):
    name, _, detail = flag.partition(":")
    text = FLAG_TEXT.get(name, name)
    return f"{text}: {detail}" if detail else text


def looks_unreliable(text):
    text = (text or "").strip()
    if len(text) < 20:
        return True
    lines = [line.strip() for line in text.splitlines() if len(line.strip()) > 10]
    if lines and max(collections.Counter(lines).values()) > 4:
        return True  # the same line over and over: the OCR model got stuck
    letters = text.replace(" ", "").replace("\n", "")
    return sum(c.isalnum() for c in letters) / max(1, len(letters)) < 0.5


def quote_found(quote, text):
    q, t = normalize(quote), normalize(text)
    if not q:
        return False
    if q in t:
        return True
    if len(q) < 6 or not t:
        return False
    match = difflib.SequenceMatcher(None, q, t, autojunk=False).find_longest_match(0, len(q), 0, len(t))
    if match.size < 4:
        return False
    start = max(0, match.b - match.a)
    window = t[start:start + len(q) + 5]
    return difflib.SequenceMatcher(None, q, window, autojunk=False).ratio() >= 0.85


def value_found(value, quote):
    v = normalize(value)
    if not v:
        return False
    d = digits(value)
    letters_and_digits = re.sub(r"\W", "", v)
    if len(d) >= 2 and len(d) >= len(letters_and_digits) / 2:
        return d in digits(quote)
    if v in normalize(quote):
        return True
    value_words, quote_words = words(value), set(words(quote))
    return bool(value_words) and sum(w in quote_words for w in value_words) / len(value_words) >= 0.8


def parse_amount(text):
    match = re.search(r"-?\d[\d.,\s]*", text or "")
    if not match:
        return None
    s = match.group(0).replace(" ", "").rstrip(".,")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", ".") if len(parts) == 2 and len(parts[-1]) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Database: progress of every page, and the search index
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY, path TEXT UNIQUE, root TEXT, out_dir TEXT, sha256 TEXT,
    size INTEGER, mtime REAL, pages INTEGER DEFAULT 0, stage TEXT DEFAULT 'new',
    profile TEXT, doc_type TEXT, title TEXT, summary TEXT, record TEXT,
    md_path TEXT, json_path TEXT, error TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS pages (
    doc_id INTEGER, page INTEGER, image TEXT, source TEXT, rotation INTEGER DEFAULT 0,
    pdf_text TEXT, ocr_text TEXT, vision TEXT, ocr_done INTEGER DEFAULT 0,
    vision_done INTEGER DEFAULT 0, final_text TEXT, text_source TEXT, agreement REAL,
    flags TEXT DEFAULT '[]', PRIMARY KEY (doc_id, page));
"""
FTS_OK = True
STAGES = ["new", "prepared", "read", "extracted", "checked", "reviewed", "done"]


def open_db():
    global FTS_OK
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DATA_DIR / "index.sqlite")
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5("
                    "doc_id UNINDEXED, page UNINDEXED, path, text)")
    except sqlite3.OperationalError:
        FTS_OK = False
    return con


def doc_row(con, doc_id):
    return con.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()


def page_rows(con, doc_id):
    return con.execute("SELECT * FROM pages WHERE doc_id = ? ORDER BY page", (doc_id,)).fetchall()


def set_stage(con, doc_id, stage, **fields):
    fields.update(stage=stage, error=None, updated=time.time())
    names = ", ".join(f"{key} = ?" for key in fields)
    con.execute(f"UPDATE documents SET {names} WHERE id = ?", (*fields.values(), doc_id))
    con.commit()


def set_error(con, doc_id, error):
    con.execute("UPDATE documents SET error = ?, updated = ? WHERE id = ?",
                (str(error)[:500], time.time(), doc_id))
    con.commit()


def docs_at(con, ids, stage):
    marks = ",".join("?" * len(ids))
    return con.execute(f"SELECT * FROM documents WHERE id IN ({marks}) AND stage = ? ORDER BY path",
                       (*ids, stage)).fetchall() if ids else []


def output_dir_for(root):
    preferred = root / OUTPUT_DIR_NAME
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return preferred
    except OSError:
        fallback = DATA_DIR / "output" / safe_name(str(root))
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def discover(target):
    if target.is_file():
        return [target] if target.suffix.lower() in SUPPORTED else []
    found = []
    for folder, dirs, files in os.walk(target):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            path = Path(folder) / name
            if path.suffix.lower() in SUPPORTED and not name.startswith("."):
                found.append(path)
    return found


def register(con, files, root, force, profile):
    """Add new or changed files. Unchanged finished files are skipped."""
    out_dir = output_dir_for(root)
    ids = []
    for path in files:
        stat = path.stat()
        row = con.execute("SELECT * FROM documents WHERE path = ?", (str(path),)).fetchone()
        if row and not force and row["size"] == stat.st_size and row["mtime"] == stat.st_mtime:
            ids.append(row["id"])
            continue
        digest = sha256_of(path)
        if row and not force and row["sha256"] == digest:
            con.execute("UPDATE documents SET size = ?, mtime = ? WHERE id = ?",
                        (stat.st_size, stat.st_mtime, row["id"]))
            ids.append(row["id"])
        elif row:  # changed, or --force: start this document again
            con.execute("DELETE FROM pages WHERE doc_id = ?", (row["id"],))
            if FTS_OK:
                con.execute("DELETE FROM search WHERE doc_id = ?", (row["id"],))
            con.execute("UPDATE documents SET sha256 = ?, size = ?, mtime = ?, stage = 'new', "
                        "record = NULL, error = NULL, root = ?, out_dir = ?, profile = ? "
                        "WHERE id = ?", (digest, stat.st_size, stat.st_mtime, str(root),
                                         str(out_dir), profile, row["id"]))
            ids.append(row["id"])
        else:
            cursor = con.execute(
                "INSERT INTO documents (path, root, out_dir, sha256, size, mtime, stage, profile, "
                "updated) VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)",
                (str(path), str(root), str(out_dir), digest, stat.st_size, stat.st_mtime,
                 profile, time.time()))
            ids.append(cursor.lastrowid)
        con.commit()
    return ids


# --------------------------------------------------------------------------
# Stage 1: prepare
# --------------------------------------------------------------------------
def stage_prepare(con, ids):
    docs = docs_at(con, ids, "new")
    job_stage("prepare", len(docs))
    for number, doc in enumerate(docs):
        path = Path(doc["path"])
        job_step(number, path.name)
        log(f"Preparing {path.name}...")
        pages_dir = Path(doc["out_dir"]) / "pages"
        try:
            pages_dir.mkdir(parents=True, exist_ok=True)
            rel = os.path.relpath(path, doc["root"])
            key = f"{safe_name(rel, 80)}-{doc['sha256'][:8]}"
            pages = prepare_document(path, pages_dir, key)
            if not pages:
                raise RuntimeError("no pages could be read from this file")
            con.execute("DELETE FROM pages WHERE doc_id = ?", (doc["id"],))
            for page in pages:
                con.execute("INSERT INTO pages (doc_id, page, image, source, rotation, pdf_text) "
                            "VALUES (?, ?, ?, ?, ?, ?)", (doc["id"], page["page"], page["image"],
                                                          page["source"], page["rotation"],
                                                          page["pdf_text"]))
                if page["rotation"]:
                    log(f"  page {page['page']}: turned {page['rotation']} degrees to read it upright")
            set_stage(con, doc["id"], "prepared", pages=len(pages))
        except Exception as error:  # keep going with the other documents
            log(f"  could not prepare {path.name}: {error}")
            set_error(con, doc["id"], f"prepare: {error}")


# --------------------------------------------------------------------------
# Stage 2: read (OCR and vision)
# --------------------------------------------------------------------------
VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "has_table": {"type": "boolean"},
        "text": {"type": "string"},
        "description": {"type": "string"},
        "unclear_parts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["kind", "has_table", "text", "description", "unclear_parts"],
}
VISION_PROMPT = (
    "Look at this image carefully and return:\n"
    "- kind: what it is\n"
    "- has_table: whether it contains a table\n"
    "- text: all readable text, exactly as written, keeping line breaks. Don't correct, "
    "translate or guess anything.\n"
    "- description: a short description of the layout and of any charts, diagrams, drawings "
    "or photos, including values shown in charts\n"
    "- unclear_parts: parts you can't read clearly")


def pending_pages(con, ids, column):
    marks = ",".join("?" * len(ids))
    return con.execute(
        f"SELECT p.*, d.path FROM pages p JOIN documents d ON d.id = p.doc_id "
        f"WHERE d.id IN ({marks}) AND d.stage = 'prepared' AND p.source = 'image' "
        f"AND p.{column} = 0 ORDER BY d.path, p.page", ids).fetchall() if ids else []


def stage_vision(con, ollama, model, ids, every_page):
    rows = pending_pages(con, ids, "vision_done")
    if not every_page:  # balanced profile: only pages the OCR model couldn't read well
        rows = [r for r in rows if r["ocr_done"] and looks_unreliable(r["ocr_text"])]
    job_stage("vision", len(rows), model)
    for number, row in enumerate(rows):
        job_step(number, f"{Path(row['path']).name}, page {row['page']}")
        log(f"Looking at {Path(row['path']).name} page {row['page']} with {model}...")
        try:
            result = ollama.chat_json(model, VISION_PROMPT, VISION_SCHEMA,
                                      [encode_image(row["image"], VISION_SIDE)], num_ctx=VISION_CTX)
            con.execute("UPDATE pages SET vision = ?, vision_done = 1 WHERE doc_id = ? AND page = ?",
                        (json.dumps(result), row["doc_id"], row["page"]))
            con.commit()
        except Exception as error:
            log(f"  {model} failed on this page: {error}")
            set_error(con, row["doc_id"], f"vision page {row['page']}: {error}")


def stage_ocr(con, ollama, model, ids, use_tables):
    rows = pending_pages(con, ids, "ocr_done")
    job_stage("ocr", len(rows), model)
    for number, row in enumerate(rows):
        job_step(number, f"{Path(row['path']).name}, page {row['page']}")
        log(f"Reading {Path(row['path']).name} page {row['page']} with {model}...")
        try:
            image = base64.b64encode(Path(row["image"]).read_bytes()).decode()
            text = ollama.chat(model, "Text Recognition:", [image], num_ctx=OCR_CTX)
            vision = json.loads(row["vision"]) if row["vision"] else {}
            if use_tables and vision.get("has_table"):
                table = ollama.chat(model, "Table Recognition:", [image], num_ctx=OCR_CTX)
                if table:
                    text += "\n\n[Table]\n" + table
            con.execute("UPDATE pages SET ocr_text = ?, ocr_done = 1 WHERE doc_id = ? AND page = ?",
                        (text, row["doc_id"], row["page"]))
            con.commit()
        except Exception as error:
            log(f"  {model} failed on this page: {error}")
            set_error(con, row["doc_id"], f"ocr page {row['page']}: {error}")


def combine_page(page, ocr_model):
    """Choose the page text and note anything doubtful."""
    if page["source"] == "pdf-text":
        return page["pdf_text"] or "", "PDF text layer", None, []
    ocr = page["ocr_text"] or ""
    vision = json.loads(page["vision"]) if page["vision"] else {}
    vision_text = vision.get("text") or ""
    flags, agreement = [], None
    if vision_text.strip() and ocr.strip():
        agreement = word_agreement(ocr, vision_text)
        if agreement < AGREEMENT_LIMIT:
            flags.append("readings_differ")
        ocr_numbers, vision_numbers = numbers_in(ocr), numbers_in(vision_text)
        differ = sorted(set(ocr_numbers) ^ set(vision_numbers))
        if differ:
            shown = [f"{ocr_numbers.get(d) or vision_numbers.get(d)} "
                     f"({'OCR' if d in ocr_numbers else 'vision'} only)" for d in differ[:10]]
            flags.append("numbers_differ:" + ", ".join(shown))
    if len(ocr.strip()) >= 20 or not vision_text.strip():
        final, source = ocr, ocr_model
    else:
        final, source = vision_text, "vision model (the OCR model found almost no text)"
        flags.append("text_from_vision")
    if looks_unreliable(ocr) and vision.get("kind") not in ("photo", "chart_or_diagram") \
            and not vision_text.strip():
        flags.append("ocr_unclear")
    if vision.get("unclear_parts"):
        flags.append("unreadable_parts")
    return final, source, agreement, flags


def stage_combine(con, ids, profile, ocr_model):
    docs = docs_at(con, ids, "prepared")
    job_stage("combine", len(docs))
    for number, doc in enumerate(docs):
        job_step(number, Path(doc["path"]).name)
        pages = page_rows(con, doc["id"])
        ready = True
        for page in pages:
            if page["source"] != "image":
                continue
            if not page["ocr_done"]:
                ready = False
            elif profile == "quality" and not page["vision_done"]:
                ready = False
            elif profile != "quality" and looks_unreliable(page["ocr_text"]) and not page["vision_done"]:
                ready = False
        if not ready:
            continue  # a page failed this time; it is tried again on the next run
        for page in pages:
            final, source, agreement, flags = combine_page(page, ocr_model)
            con.execute("UPDATE pages SET final_text = ?, text_source = ?, agreement = ?, flags = ? "
                        "WHERE doc_id = ? AND page = ?", (final, source, agreement,
                                                          json.dumps(flags), doc["id"], page["page"]))
        set_stage(con, doc["id"], "read")


# --------------------------------------------------------------------------
# Stage 3: extract (data-processing model)
# --------------------------------------------------------------------------
ITEM = {"type": "object",
        "properties": {"label": {"type": "string"}, "value": {"type": "string"},
                       "page": {"type": "integer"}, "quote": {"type": "string"}},
        "required": ["label", "value", "page", "quote"]}
LINE = {"type": "object",
        "properties": {"description": {"type": "string"}, "quantity": {"type": "string"},
                       "unit_price": {"type": "string"}, "amount": {"type": "string"},
                       "page": {"type": "integer"}, "quote": {"type": "string"}},
        "required": ["description", "amount", "page", "quote"]}
EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": KINDS},
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "fields": {"type": "array", "items": ITEM},
        "line_items": {"type": "array", "items": LINE},
        "missing_or_unclear": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["document_type", "title", "summary", "fields", "line_items", "missing_or_unclear"],
}
EXTRACT_SYSTEM = """You extract information from documents.
Rules:
- Use only the page text you are given. Never guess or invent a value.
- If something you would expect is missing or unreadable, list it in missing_or_unclear instead of filling it in.
- For every field and line item, give the page number and an exact quote copied from that page's text that contains the value.
- Copy values exactly as written: the same numbers, spelling and date format. The text comes from OCR and may contain mistakes; don't correct numbers.
- Use short, plain labels such as "date", "total", "store name" or "invoice number"."""


def page_block(page):
    block = f"=== Page {page['page']} ===\n{page['final_text'] or ''}"
    vision = json.loads(page["vision"]) if page["vision"] else {}
    if vision.get("description"):
        block += f"\n[Image description: {vision['description']}]"
    return block


def extraction_chunks(pages):
    chunks, current, size = [], [], 0
    for page in pages:
        block = page_block(page)
        if current and size + len(block) > CHUNK_CHARS:
            chunks.append(current)
            current, size = [], 0
        current.append(block)
        size += len(block)
    if current:
        chunks.append(current)
    return chunks


def merge_records(parts):
    types = collections.Counter(p.get("document_type", "other") for p in parts)
    record = {"document_type": types.most_common(1)[0][0] if types else "other",
              "title": next((p.get("title") for p in parts if p.get("title")), ""),
              "summary": " ".join(p.get("summary", "") for p in parts if p.get("summary")).strip(),
              "fields": [], "line_items": [], "missing_or_unclear": []}
    for part in parts:
        record["fields"] += [f for f in part.get("fields", []) if isinstance(f, dict)]
        record["line_items"] += [li for li in part.get("line_items", []) if isinstance(li, dict)]
        for note in part.get("missing_or_unclear", []):
            if note not in record["missing_or_unclear"]:
                record["missing_or_unclear"].append(note)
    return record


def stage_extract(con, ollama, model, ids):
    hints = "\n".join(f"- {kind}: {', '.join(items)}" for kind, items in fields_config().items()
                      if items)
    docs = docs_at(con, ids, "read")
    job_stage("extract", len(docs), model)
    for number, doc in enumerate(docs):
        name = Path(doc["path"]).name
        job_step(number, name)
        log(f"Extracting values from {name} with {model}...")
        try:
            parts = []
            for chunk in extraction_chunks(page_rows(con, doc["id"])):
                prompt = (f"File: {name}\n\nIf the document is one of these types, look for these "
                          f"fields:\n{hints}\n\nAlso extract any other important values you see."
                          "\n\n" + "\n\n".join(chunk))
                parts.append(ollama.chat_json(model, prompt, EXTRACT_SCHEMA, system=EXTRACT_SYSTEM))
            record = merge_records(parts)
            record["extracted_by"] = model
            set_stage(con, doc["id"], "extracted", record=json.dumps(record),
                      doc_type=record["document_type"], title=record["title"],
                      summary=record["summary"])
        except Exception as error:
            log(f"  extraction failed: {error}")
            set_error(con, doc["id"], f"extract: {error}")


# --------------------------------------------------------------------------
# Stage 4: check every value against its page
# --------------------------------------------------------------------------
def items_of(record):
    return list(record.get("fields", [])) + list(record.get("line_items", []))


def label_of(item):
    return item.get("label") or item.get("description") or ""


def value_key(item):
    return "value" if "value" in item else "amount"


def check_item(item, texts):
    page = item.get("page")
    quote = item.get("quote") or ""
    value = item.get(value_key(item)) or ""
    if page not in texts or not quote_found(quote, texts[page]):
        other = next((n for n, t in texts.items() if quote_found(quote, t)), None)
        if other is None:
            return "unverified", "the quote wasn't found on any page"
        item["page"] = other
    if not value_found(value, quote):
        return "unverified", "the value isn't in its quote"
    return "verified", ""


def check_totals(record):
    """Do the line items add up to the subtotal or total? Returns a list of problems."""
    amounts = [parse_amount(li.get("amount")) for li in record.get("line_items", [])]
    if len(amounts) < 2 or any(a is None for a in amounts):
        return []
    total_sum = round(sum(amounts), 2)

    def find(pattern, exclude=None):
        for item in record.get("fields", []):
            label = item.get("label", "")
            if re.search(pattern, label, re.I) and not (exclude and re.search(exclude, label, re.I)):
                number = parse_amount(item.get("value"))
                if number is not None:
                    return item, number
        return None, None

    sub_item, subtotal = find(r"sub\s*-?total")
    total_item, total = find(r"\btotal\b", exclude=r"sub")
    tax_item, tax = find(r"\b(tax|vat|gst|hst)\b")
    tolerance = max(0.02, 0.005 * total_sum)
    if subtotal is not None:
        if abs(subtotal - total_sum) > tolerance:
            return [f"Line items add up to {total_sum:.2f}, but the {sub_item['label']} is "
                    f"{sub_item['value']} (page {sub_item.get('page')})."]
        return []
    if total is not None and abs(total - total_sum) > tolerance:
        if tax is not None and abs(total - (total_sum + tax)) <= tolerance:
            return []
        return [f"Line items add up to {total_sum:.2f}, but the {total_item['label']} is "
                f"{total_item['value']} (page {total_item.get('page')})."]
    return []


def stage_check(con, ids, profile):
    docs = docs_at(con, ids, "extracted")
    job_stage("check", len(docs))
    for number, doc in enumerate(docs):
        job_step(number, Path(doc["path"]).name)
        pages = page_rows(con, doc["id"])
        texts = {p["page"]: p["final_text"] or "" for p in pages}
        doubtful_pages = {p["page"] for p in pages
                          if any(f.startswith(DOUBTFUL_FLAGS) for f in json.loads(p["flags"] or "[]"))}
        record = json.loads(doc["record"])
        for item in items_of(record):
            item["status"], item["check_note"] = check_item(item, texts)
            item["needs_review"] = item["status"] != "verified" or (
                profile == "quality" and item.get("page") in doubtful_pages)
        record["discrepancies"] = check_totals(record)
        set_stage(con, doc["id"], "checked", record=json.dumps(record))


# --------------------------------------------------------------------------
# Stage 5: review doubtful values against the page image
# --------------------------------------------------------------------------
REVIEW_SCHEMA = {
    "type": "object",
    "properties": {"checks": {"type": "array", "items": {
        "type": "object",
        "properties": {"number": {"type": "integer"},
                       "verdict": {"type": "string", "enum": ["correct", "wrong", "not_visible"]},
                       "correct_value": {"type": "string"}, "quote": {"type": "string"},
                       "note": {"type": "string"}},
        "required": ["number", "verdict", "correct_value", "quote", "note"]}}},
    "required": ["checks"],
}
REVIEW_SYSTEM = """You check values that were extracted from a document page, using the page itself.
For each numbered value, answer:
- correct: the page shows this value exactly.
- wrong: the page shows a different value. Give the correct value and an exact quote from the page.
- not_visible: you can't find it on the page.
Never guess. If you aren't sure, answer not_visible."""


def stage_review(con, ollama, model, ids, enabled):
    can_see = ollama.can_see(model) if enabled else False
    docs = docs_at(con, ids, "checked")
    job_stage("review", len(docs), model if enabled else None)
    for number, doc in enumerate(docs):
        record = json.loads(doc["record"])
        todo = [item for item in items_of(record) if item.get("needs_review")]
        job_step(number, Path(doc["path"]).name)
        if not enabled or not todo:
            set_stage(con, doc["id"], "reviewed")
            continue
        name = Path(doc["path"]).name
        log(f"Reviewing {len(todo)} value(s) in {name} with {model}...")
        pages = {p["page"]: p for p in page_rows(con, doc["id"])}
        try:
            by_page = collections.defaultdict(list)
            for item in todo:
                by_page[item.get("page")].append(item)
            for number, entries in by_page.items():
                page = pages.get(number)
                if page is None:
                    for item in entries:
                        item["status"], item["needs_review"] = "not_found_on_page", False
                    continue
                listing = "\n".join(f"{i}. {label_of(item)}: {item.get(value_key(item))} "
                                    f'(quote: "{item.get("quote", "")}")'
                                    for i, item in enumerate(entries, 1))
                prompt = (f"Page {number} of {name}. Text read from the page:\n"
                          f"{page['final_text']}\n\nValues to check:\n{listing}")
                images = [encode_image(page["image"], REVIEW_SIDE)] if can_see else []
                result = ollama.chat_json(model, prompt, REVIEW_SCHEMA, images, REVIEW_SYSTEM)
                for check in result.get("checks", []):
                    index = check.get("number", 0) - 1
                    if not 0 <= index < len(entries):
                        continue
                    item = entries[index]
                    item["review_note"] = check.get("note", "")
                    verdict = check.get("verdict")
                    if verdict == "correct":
                        item["status"] = "verified_by_review" if item["status"] != "verified" \
                            else "verified"
                    elif verdict == "wrong" and check.get("correct_value"):
                        key = value_key(item)
                        item["original_value"] = item.get(key)
                        item[key] = check["correct_value"]
                        item["quote"] = check.get("quote") or item.get("quote")
                        item["status"] = "corrected_by_review"
                    elif verdict == "not_visible":
                        # Found in the text but not seen by the reviewer: the two sources
                        # disagree, so a person should look. Never found at all: not on the page.
                        item["status"] = "disputed" if item["status"] == "verified" \
                            else "not_found_on_page"
                    item["needs_review"] = False
            record["reviewed_by"] = model
            record["discrepancies"] = check_totals(record)
            set_stage(con, doc["id"], "reviewed", record=json.dumps(record))
        except Exception as error:
            log(f"  review failed: {error}")
            set_error(con, doc["id"], f"review: {error}")


# --------------------------------------------------------------------------
# Stage 6: save Markdown, JSON and the search index
# --------------------------------------------------------------------------
def cell(text):
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def write_outputs(con, doc, profile, models):
    record = json.loads(doc["record"])
    pages = page_rows(con, doc["id"])
    path = Path(doc["path"])
    out_dir = Path(doc["out_dir"])
    rel = os.path.relpath(path, doc["root"])
    md_path, json_path = out_dir / (rel + ".md"), out_dir / (rel + ".json")
    md_path.parent.mkdir(parents=True, exist_ok=True)

    page_info = []
    for page in pages:
        vision = json.loads(page["vision"]) if page["vision"] else {}
        page_info.append({"page": page["page"], "image": page["image"],
                          "turned_degrees": page["rotation"], "text_source": page["text_source"],
                          "readings_agreement": page["agreement"],
                          "flags": json.loads(page["flags"] or "[]"),
                          "kind_seen": vision.get("kind"), "description": vision.get("description"),
                          "text": page["final_text"]})
    result = {"source": str(path), "sha256": doc["sha256"], "processed": now_text(),
              "profile": profile, "models": models, **record, "pages": page_info}
    json_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")

    lines = [f"# {record.get('title') or path.name}", "",
             f"Source: {path}", f"Type: {record.get('document_type')} | Pages: {len(pages)} | "
             f"Profile: {profile} | Processed: {now_text()}", ""]
    if record.get("summary"):
        lines += [f"Summary: {record['summary']}", ""]
    if record.get("fields"):
        lines += ["## Values", "", "| Label | Value | Page | Check |", "|---|---|---|---|"]
        lines += [f"| {cell(f.get('label'))} | {cell(f.get('value'))} | {cell(f.get('page'))} | "
                  f"{cell(f.get('status'))} |" for f in record["fields"]]
        lines.append("")
    if record.get("line_items"):
        lines += ["## Line items", "", "| Description | Qty | Unit price | Amount | Page | Check |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {cell(li.get('description'))} | {cell(li.get('quantity'))} | "
                  f"{cell(li.get('unit_price'))} | {cell(li.get('amount'))} | {cell(li.get('page'))} | "
                  f"{cell(li.get('status'))} |" for li in record["line_items"]]
        lines.append("")
    problems = [f"Missing or unclear: {note}" for note in record.get("missing_or_unclear", [])]
    problems += [f"Discrepancy: {note}" for note in record.get("discrepancies", [])]
    problems += [f"{label_of(i)} = {i.get(value_key(i))} (page {i.get('page')}): {i.get('status')}"
                 for i in items_of(record) if i.get("status") not in GOOD]
    problems += [f"Page {p['page']}: {describe_flag(flag)}" for p in page_info for flag in p["flags"]]
    if problems:
        lines += ["## Needs attention", ""] + [f"- {p}" for p in problems] + [""]
    for page in page_info:
        lines += [f"## Page {page['page']}", "", f"Image: {page['image']}"]
        if page["turned_degrees"]:
            lines.append(f"Turned {page['turned_degrees']} degrees to read it upright.")
        agreement = page["readings_agreement"]
        lines.append(f"Text from: {page['text_source']}" +
                     (f" (the two readings agree {agreement:.0%})" if agreement is not None else ""))
        lines += ["", page["text"] or "(no text found)", ""]
        if page["description"]:
            lines += [f"Image description: {page['description']}", ""]
    md_path.write_text("\n".join(lines))

    if FTS_OK:
        con.execute("DELETE FROM search WHERE doc_id = ?", (doc["id"],))
        head = " ".join([record.get("title", ""), record.get("summary", ""),
                         record.get("document_type", "")] +
                        [f"{label_of(i)}: {i.get(value_key(i))}" for i in items_of(record)])
        con.execute("INSERT INTO search (doc_id, page, path, text) VALUES (?, 0, ?, ?)",
                    (doc["id"], str(path), head))
        for page in page_info:
            con.execute("INSERT INTO search (doc_id, page, path, text) VALUES (?, ?, ?, ?)",
                        (doc["id"], page["page"], str(path),
                         (page["text"] or "") + "\n" + (page["description"] or "")))
    set_stage(con, doc["id"], "done", md_path=str(md_path), json_path=str(json_path))


def stage_write(con, ids, profile, models):
    docs = docs_at(con, ids, "reviewed")
    job_stage("write", len(docs))
    for number, doc in enumerate(docs):
        job_step(number, Path(doc["path"]).name)
        try:
            write_outputs(con, doc, profile, models)
        except Exception as error:
            log(f"  could not save the results for {Path(doc['path']).name}: {error}")
            set_error(con, doc["id"], f"save: {error}")


# --------------------------------------------------------------------------
# Running the pipeline
# --------------------------------------------------------------------------
class Lock:
    """Only one processing job at a time, so models don't fight over the GPU."""

    def __init__(self):
        self.path = DATA_DIR / "process.lock"

    def __enter__(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        pid = running_job()
        if pid and pid != os.getpid():
            sys.exit(f"Another hermes-docs job is running (process {pid}). "
                     "See its progress with: hermes-docs status")
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            if self.path.read_text().strip() == str(os.getpid()):
                self.path.unlink()
        except OSError:
            pass


def pid_alive(pid):
    """Whether a process is running. (On Windows os.kill would END the process, so ask Windows.)"""
    if WINDOWS:
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def running_job():
    try:
        pid = int((DATA_DIR / "process.lock").read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid_alive(pid) else None


def check_models(ollama, models, review):
    try:
        installed = ollama.installed()
    except RuntimeError as error:
        sys.exit(f"Ollama isn't running ({error}). " + ("Start 'Ollama' from the Start menu." if WINDOWS
                 else "Start it with: sudo systemctl start ollama"))
    needed = ["ocr", "vision", "processor"] + (["reviewer"] if review else [])
    missing = [models[role] for role in needed if full_name(models[role]) not in installed]
    if missing:
        sys.exit("These models aren't in Ollama: " + ", ".join(missing) +
                 "\nRun the setup again: SETUP_WINDOWS.bat")


def run_pipeline(con, ollama, models, ids, profile, review=True, until=None):
    stage_prepare(con, ids)
    if profile == "quality":
        stage_vision(con, ollama, models["vision"], ids, every_page=True)
        stage_ocr(con, ollama, models["ocr"], ids, use_tables=True)
    else:
        stage_ocr(con, ollama, models["ocr"], ids, use_tables=False)
        stage_vision(con, ollama, models["vision"], ids, every_page=False)
    stage_combine(con, ids, profile, models["ocr"])
    if until == "read":
        return
    stage_extract(con, ollama, models["processor"], ids)
    stage_check(con, ids, profile)
    if until == "checked":
        return
    stage_review(con, ollama, models["reviewer"], ids, review)
    stage_write(con, ids, profile, models)


def report(con, ids):
    rows = [doc_row(con, i) for i in ids]
    done = [r for r in rows if r["stage"] == "done"]
    statuses = collections.Counter()
    attention = []
    for row in done:
        record = json.loads(row["record"])
        items = items_of(record)
        statuses.update(i.get("status") for i in items)
        if record.get("discrepancies") or record.get("missing_or_unclear") or \
                any(i.get("status") not in GOOD for i in items):
            attention.append(row["md_path"])
    log("")
    log(f"Finished: {len(done)} of {len(rows)} documents done.")
    log(f"Values: {statuses['verified'] + statuses['verified_by_review']} found on the page, "
        f"{statuses['corrected_by_review']} corrected by review, "
        f"{statuses['disputed']} disputed, "
        f"{statuses['unverified'] + statuses['not_found_on_page']} not confirmed.")
    if attention:
        log(f"{len(attention)} document(s) need attention (see 'Needs attention' in each file):")
        for path in attention[:20]:
            log(f"  {path}")
    failed = [r for r in rows if r["stage"] != "done"]
    for row in failed[:20]:
        log(f"Not finished: {row['path']} ({row['error'] or 'stopped at ' + row['stage']})")
    if failed:
        log("Run the same command again to continue; finished work is kept.")
    folders = sorted({r["out_dir"] for r in done})
    for folder in folders:
        log(f"Results are in: {folder}")
    job_save(force=True, finished=True, summary={
        "documents_done": len(done), "documents_total": len(rows),
        "found": statuses["verified"] + statuses["verified_by_review"],
        "corrected": statuses["corrected_by_review"], "disputed": statuses["disputed"],
        "not_confirmed": statuses["unverified"] + statuses["not_found_on_page"],
        "attention": attention[:200], "folders": folders,
        "not_finished": [{"path": r["path"], "reason": r["error"] or "stopped at " + r["stage"]}
                         for r in failed[:200]]})


def new_job(target, profile, pid):
    return {"pid": pid, "target": str(target), "profile": profile, "started": time.time(),
            "stage_number": 0, "stage_total": len(STAGE_TITLES), "stage_title": "Starting",
            "done": 0, "total": 0, "current": "", "finished": False}


def stop_on_terminate(signum, frame):
    raise KeyboardInterrupt


def cmd_process(args):
    global LOG_HANDLE
    settings = load_settings()
    profile = args.profile or settings["profile"]
    target = Path(args.path).expanduser().resolve()
    if not target.exists():
        sys.exit(f"Not found: {target}")
    if args.background:
        start_background(args, profile)
        return
    child = os.environ.get("HERMES_DOCS_CHILD")
    started = read_job()
    if child and started.get("token") and started["token"] == os.environ.get("HERMES_DOCS_JOB"):
        JOB.update(started, pid=os.getpid())  # the job that --background (or the app) started
        job_save(force=True)
    if not child:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        LOG_HANDLE = open(DATA_DIR / "process.log", "a")
        LOG_HANDLE.write(f"\n=== {now_text()} process {target} ({profile})\n")
    signal.signal(signal.SIGTERM, stop_on_terminate)  # "Stop" in the app: stop cleanly
    try:
        ollama = Ollama(settings["ollama"])
        check_models(ollama, settings["models"], not args.no_review)
        files = discover(target)
        if not files:
            sys.exit(f"No photos, images or PDFs found at {target}")
        con = open_db()
        with Lock():
            if not JOB:
                JOB.update(new_job(target, profile, os.getpid()))
            job_save(force=True, files=len(files))
            root = target if target.is_dir() else target.parent
            ids = register(con, files, root, args.force, profile)
            log(f"{len(files)} file(s) found. Profile: {profile}.")
            run_pipeline(con, ollama, settings["models"], ids, profile, review=not args.no_review)
            report(con, ids)
    except SystemExit as stop:
        if isinstance(stop.code, str):
            job_save(force=True, finished=True, error=stop.code)
        raise
    except KeyboardInterrupt:
        job_save(force=True, finished=True, error="Stopped. Start it again to continue; "
                                                  "finished work is kept.")
        raise
    except Exception as error:
        job_save(force=True, finished=True, error=f"Stopped by an error: {error}")
        raise


def start_background(args, profile):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if running_job():
        sys.exit(f"Another hermes-docs job is running (process {running_job()}). "
                 "See its progress with: hermes-docs status")
    target = Path(args.path).expanduser().resolve()
    command = [sys.executable, str(Path(__file__).resolve()), "process", str(target),
               "--profile", profile]
    command += ["--force"] if args.force else []
    command += ["--no-review"] if args.no_review else []
    log_file = open(DATA_DIR / "process.log", "a")
    log_file.write(f"\n=== {now_text()} process {target} ({profile}, in the background)\n")
    log_file.flush()
    token = f"{os.getpid()}-{time.time()}"
    JOB.update(new_job(target, profile, None), token=token)  # the job fills in its own pid
    job_save(force=True)
    detach = ({"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | NEW_PROCESS_GROUP
              if WINDOWS else {"start_new_session": True})
    process = subprocess.Popen(command, stdout=log_file, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, **detach,
                               env=dict(os.environ, HERMES_DOCS_CHILD="1", HERMES_DOCS_JOB=token))
    print(f"Started in the background (process {process.pid}).")
    print("Check progress with: hermes-docs status")


# --------------------------------------------------------------------------
# status, search, show, forget
# --------------------------------------------------------------------------
def cmd_status(args):
    con = open_db()
    counts = dict(con.execute("SELECT stage, COUNT(*) FROM documents GROUP BY stage").fetchall())
    total = sum(counts.values())
    if getattr(args, "json", False):
        errors = con.execute("SELECT path, error FROM documents WHERE error IS NOT NULL "
                             "ORDER BY updated DESC LIMIT 50").fetchall()
        print(json.dumps({"documents": total, "stages": counts, "running": running_job(),
                          "problems": [dict(row) for row in errors], "job": read_job(),
                          "log": str(DATA_DIR / "process.log"),
                          "fields": str(DATA_DIR / "fields.json")}, indent=2))
        return
    print(f"Documents in the index: {total}")
    for stage in STAGES:
        if counts.get(stage):
            print(f"  {stage:10} {counts[stage]}")
    pid = running_job()
    print(f"A job is running now (process {pid})." if pid else "No job is running.")
    errors = con.execute("SELECT path, error FROM documents WHERE error IS NOT NULL "
                         "ORDER BY updated DESC LIMIT 10").fetchall()
    if errors:
        print("Recent problems:")
        for row in errors:
            print(f"  {row['path']}: {row['error']}")
    log_path = DATA_DIR / "process.log"
    if log_path.exists():
        print("Latest log lines:")
        for line in log_path.read_text(errors="replace").splitlines()[-12:]:
            print(f"  {line}")


def search_rows(con, query, limit):
    terms = re.findall(r"\w+", query)
    if not terms:
        return []
    if FTS_OK:
        for joiner in (" ", " OR "):
            match = joiner.join(f'"{t}"' for t in terms)
            rows = con.execute(
                "SELECT search.doc_id, search.page, snippet(search, 3, '[', ']', ' ... ', 14) AS snip, "
                "d.path, d.doc_type, d.json_path FROM search JOIN documents d ON d.id = search.doc_id "
                "WHERE search MATCH ? ORDER BY bm25(search) LIMIT ?", (match, limit)).fetchall()
            if rows:
                return [dict(r) for r in rows]
        return []
    like = f"%{query}%"
    rows = con.execute("SELECT p.doc_id, p.page, substr(p.final_text, 1, 160) AS snip, d.path, "
                       "d.doc_type, d.json_path FROM pages p JOIN documents d ON d.id = p.doc_id "
                       "WHERE p.final_text LIKE ? LIMIT ?", (like, limit)).fetchall()
    return [dict(r) for r in rows]


def cmd_search(args):
    rows = search_rows(open_db(), args.query, args.limit)
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return
    if not rows:
        print("Nothing found.")
        return
    for number, row in enumerate(rows, 1):
        where = "summary and values" if row["page"] == 0 else f"page {row['page']}"
        print(f"{number}. {row['path']}  ({where}, {row['doc_type'] or 'unknown type'})")
        print(f"   {' '.join((row['snip'] or '').split())}")
        if row["json_path"]:
            print(f"   Details: {row['json_path']}")


def find_document(con, name):
    path = Path(name).expanduser()
    row = con.execute("SELECT * FROM documents WHERE path = ?", (str(path.resolve()),)).fetchone()
    if row:
        return row
    rows = con.execute("SELECT * FROM documents WHERE path LIKE ?", (f"%{path.name}",)).fetchall()
    if len(rows) == 1:
        return rows[0]
    if rows:
        sys.exit("Several documents match. Use the full path:\n" +
                 "\n".join(f"  {r['path']}" for r in rows[:20]))
    sys.exit(f"{name} isn't in the index. Process it first: hermes-docs process \"{name}\"")


def cmd_show(args):
    con = open_db()
    row = find_document(con, args.file)
    if args.page:
        page = con.execute("SELECT * FROM pages WHERE doc_id = ? AND page = ?",
                           (row["id"], args.page)).fetchone()
        if not page:
            sys.exit(f"{row['path']} has no page {args.page} (it has {row['pages']}).")
        print(f"{row['path']}, page {args.page}")
        print(f"Image: {page['image']}")
        flags = [describe_flag(f) for f in json.loads(page["flags"] or "[]")]
        print(f"Text from: {page['text_source']}")
        print(f"Notes: {'; '.join(flags) if flags else 'none'}")
        print()
        print(page["final_text"] or page["ocr_text"] or "(not read yet)")
        return
    if args.json and row["json_path"] and Path(row["json_path"]).exists():
        print(Path(row["json_path"]).read_text())
    elif row["md_path"] and Path(row["md_path"]).exists():
        print(Path(row["md_path"]).read_text())
    else:
        print(f"{row['path']} isn't finished yet (stage: {row['stage']}). "
              f"Continue with: hermes-docs process \"{row['path']}\"")


def same_path_key(path):
    """A path in one form for comparing: on Windows "/" and "\\" mean the same, and case doesn't matter."""
    return os.path.normcase(os.path.normpath(str(path)))


def cmd_forget(args):
    con = open_db()
    target = same_path_key(Path(args.path).expanduser().resolve())
    folder = target.rstrip(os.sep) + os.sep          # a file inside the folder, at any depth
    rows = [row for row in con.execute("SELECT id, path FROM documents").fetchall()
            if same_path_key(row["path"]) == target or same_path_key(row["path"]).startswith(folder)]
    for row in rows:
        con.execute("DELETE FROM pages WHERE doc_id = ?", (row["id"],))
        if FTS_OK:
            con.execute("DELETE FROM search WHERE doc_id = ?", (row["id"],))
        con.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
    con.commit()
    print(f"Removed {len(rows)} document(s) from the index. The files themselves were not touched.")


# --------------------------------------------------------------------------
# Test images with known values: selftest, benchmark, samples
# --------------------------------------------------------------------------
SAMPLES = [
    {"name": "receipt.png", "turn": 0, "exif": None, "mono": True,
     "lines": ["MAPLE STREET HARDWARE", "12 Maple Street, Springfield",
               "Date: 2026-03-14   Time: 10:42", "",
               "Hammer           1 x 12.99    12.99", "Box of nails     2 x  4.50     9.00",
               "Paint brush      1 x  7.25     7.25", "",
               "Subtotal                      29.24", "Tax 8%                         2.34",
               "TOTAL                         31.58", "Paid by VISA ending 4417"],
     "expect": {"store name": "Maple Street Hardware", "date": "2026-03-14", "subtotal": "29.24",
                "tax": "2.34", "total": "31.58"}},
    {"name": "invoice_sideways.jpg", "turn": 90, "exif": None, "mono": False,
     "lines": ["INVOICE INV-20417", "Northwind Supplies", "Invoice date: 2026-05-02",
               "Bill to: Riverside Workshop", "", "Steel brackets   40 x 30.00   1,200.00",
               "Shipping                       280.00", "", "Total due: 1,480.00 USD",
               "Due date: 2026-06-01"],
     "expect": {"invoice number": "INV-20417", "seller": "Northwind Supplies",
                "total": "1,480.00", "due date": "2026-06-01"}},
    {"name": "form_phone_photo.jpg", "turn": 90, "exif": 6, "mono": False,
     "lines": ["SERVICE REQUEST FORM", "Name: Alex Rivera", "Phone: 555-0142", "Device: Laptop",
               "Problem: Screen flickers", "Date: 2026-07-21"],
     "expect": {"name": "Alex Rivera", "phone": "555-0142", "date": "2026-07-21"}},
]


def sample_font(size, mono):
    from PIL import ImageFont
    names = ["DejaVuSansMono.ttf", "DejaVuSans.ttf"] if mono else ["DejaVuSans.ttf"]
    folders = [Path("/usr/share/fonts/truetype/dejavu")]
    if WINDOWS:
        names = (["consola.ttf", "cour.ttf"] if mono else []) + ["arial.ttf", "segoeui.ttf"]
        folders = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    for folder in folders:
        for name in names:
            path = folder / name
            if path.exists():
                return ImageFont.truetype(str(path), size)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def make_samples(folder):
    """Three test images: an upright receipt, an invoice photographed sideways, and a form
    whose phone photo is stored sideways with the camera's rotation setting."""
    Image, _ = pil()
    from PIL import ImageDraw
    folder.mkdir(parents=True, exist_ok=True)
    for sample in SAMPLES:
        image = Image.new("RGB", (1400, 1100), "white")
        draw = ImageDraw.Draw(image)
        font = sample_font(40, sample["mono"])
        for index, line in enumerate(sample["lines"]):
            draw.text((70, 60 + index * 72), line, fill="black", font=font)
        stored = image.rotate(sample["turn"], expand=True, fillcolor="white")
        target = folder / sample["name"]
        if sample["exif"]:
            exif = Image.Exif()
            exif[0x0112] = sample["exif"]  # orientation 6: show turned 90 degrees clockwise
            stored.save(target, "JPEG", quality=92, exif=exif.tobytes())
        elif target.suffix == ".jpg":
            stored.save(target, "JPEG", quality=92)
        else:
            stored.save(target, "PNG")
    return [folder / s["name"] for s in SAMPLES]


def value_matches(want, got):
    if len(digits(want)) >= 3:
        return digits(want) == digits(got)
    return normalize(want) in normalize(got) or (normalize(got) and normalize(got) in normalize(want))


def evaluate(con, folder):
    results = []
    for sample in SAMPLES:
        row = con.execute("SELECT * FROM documents WHERE path = ?",
                          (str((folder / sample["name"]).resolve()),)).fetchone()
        entry = {"file": sample["name"], "finished": bool(row and row["record"])}
        if not entry["finished"]:
            entry["error"] = row["error"] if row else "not processed"
            results.append(entry)
            continue
        pages = page_rows(con, row["id"])
        text = "\n".join(p["final_text"] or "" for p in pages)
        # Measure the reading itself: in the quality profile a page with a table also gets a
        # second "[Table]" reading (often HTML), which repeats the same words and would make
        # the score look lower than it is. The table reading is still kept for extraction.
        plain = "\n".join(re.sub(r"<[^>]+>", " ", (p["final_text"] or "").split("\n\n[Table]\n")[0])
                          for p in pages)
        entry["ocr_accuracy"] = round(word_agreement(plain, "\n".join(sample["lines"])), 3)
        upright = [tesseract_rotation(p["image"]) for p in pages]
        entry["upright"] = None if any(u is None for u in upright) else all(u[0] == 0 for u in upright)
        items = items_of(json.loads(row["record"]))
        found = {key: any(value_matches(want, str(i.get(value_key(i), ""))) for i in items)
                 for key, want in sample["expect"].items()}
        confirmed = {key: any(value_matches(want, str(i.get(value_key(i), "")))
                              and i.get("status") in GOOD for i in items)
                     for key, want in sample["expect"].items()}
        entry["values_found"] = sum(found.values())
        entry["values_confirmed"] = sum(confirmed.values())
        entry["values_expected"] = len(sample["expect"])
        entry["missed"] = [k for k, v in found.items() if not v]
        results.append(entry)
    return results


def run_test_pipeline(keep, until=None, processor=None):
    global DATA_DIR
    settings = load_settings()
    models = dict(settings["models"])
    if processor:
        models["processor"] = processor
    work = Path(tempfile.mkdtemp(prefix="hermes-docs-test-"))
    DATA_DIR = work / "data"
    folder = work / "documents"
    make_samples(folder)
    ollama = Ollama(settings["ollama"])
    check_models(ollama, models, until is None)
    con = open_db()
    ids = register(con, discover(folder), folder, False, settings["profile"])
    return work, folder, con, ollama, models, settings["profile"], ids


def cmd_selftest(args):
    started = time.time()
    work, folder, con, ollama, models, profile, ids = run_test_pipeline(args.keep)
    run_pipeline(con, ollama, models, ids, profile)
    results = evaluate(con, folder)
    found = search_rows(con, "Northwind", 5)
    summary = {
        "profile": profile, "models": models, "seconds": round(time.time() - started),
        "documents": results,
        "ocr_accuracy": round(sum(r.get("ocr_accuracy", 0) for r in results) / len(results), 3),
        "upright": all(r.get("upright") is not False for r in results),
        "values_expected": sum(r.get("values_expected", 0) for r in results),
        "values_found": sum(r.get("values_found", 0) for r in results),
        "values_confirmed": sum(r.get("values_confirmed", 0) for r in results),
        "search_works": any(r["path"].endswith("invoice_sideways.jpg") for r in found),
        "all_finished": all(r["finished"] for r in results),
    }
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    else:
        summary["folder"] = str(work)
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    print(f"OCR accuracy: {summary['ocr_accuracy']:.0%}")
    print(f"Photos turned upright: {'yes' if summary['upright'] else 'no'}")
    print(f"Values found: {summary['values_found']} of {summary['values_expected']} "
          f"({summary['values_confirmed']} confirmed on the page)")
    for r in results:
        if r.get("missed"):
            print(f"  Not found in {r['file']}: {', '.join(r['missed'])}")
        elif r.get("error"):
            print(f"  {r['file']}: {r['error']}")
    print(f"Search works: {'yes' if summary['search_works'] else 'no'}")
    print(f"Time: {summary['seconds']} seconds")


def cmd_benchmark(args):
    processors = [p.strip() for p in args.processors.split(",") if p.strip()]
    work, folder, con, ollama, models, profile, ids = run_test_pipeline(False, until="read")
    log("Reading the test images once (the same text is given to every model)...")
    run_pipeline(con, ollama, models, ids, profile, until="read")
    results = []
    for processor in processors:
        log(f"Testing {processor}...")
        con.execute(f"UPDATE documents SET stage = 'read', record = NULL "
                    f"WHERE id IN ({','.join('?' * len(ids))})", ids)
        con.commit()
        started = time.time()
        stage_extract(con, ollama, processor, ids)
        stage_check(con, ids, profile)
        scores = evaluate(con, folder)
        results.append({
            "processor": processor, "seconds": round(time.time() - started),
            "values_expected": sum(s.get("values_expected", 0) for s in scores),
            "values_found": sum(s.get("values_found", 0) for s in scores),
            "values_confirmed": sum(s.get("values_confirmed", 0) for s in scores),
            "finished": all(s["finished"] for s in scores)})
    shutil.rmtree(work, ignore_errors=True)
    if args.json:
        print(json.dumps(results, indent=2))
        return
    print(f"{'Model':32} {'Found':>7} {'Confirmed':>10} {'Time':>8}")
    for r in results:
        print(f"{r['processor']:32} {r['values_found']:>3}/{r['values_expected']:<3} "
              f"{r['values_confirmed']:>6}/{r['values_expected']:<3} {r['seconds']:>6}s")


def cmd_samples(args):
    for path in make_samples(Path(args.folder).expanduser().resolve()):
        print(path)


def main():
    if WINDOWS:   # Windows consoles often can't print every character; never crash on that
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass
    parser = argparse.ArgumentParser(prog="hermes-docs", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("process", help="read and check documents")
    p.add_argument("path")
    p.add_argument("--profile", choices=["quality", "balanced"])
    p.add_argument("--background", action="store_true", help="run as a background job")
    p.add_argument("--force", action="store_true", help="process finished files again")
    p.add_argument("--no-review", action="store_true", help="skip the review stage")
    p.set_defaults(func=cmd_process)
    p = commands.add_parser("status", help="progress and problems")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    p = commands.add_parser("search", help="find processed documents")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)
    p = commands.add_parser("show", help="show the results for one document")
    p.add_argument("file")
    p.add_argument("--page", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)
    p = commands.add_parser("forget", help="remove documents from the index")
    p.add_argument("path")
    p.set_defaults(func=cmd_forget)
    p = commands.add_parser("selftest", help="test the pipeline on images with known values")
    p.add_argument("--json", action="store_true")
    p.add_argument("--keep", action="store_true", help="keep the test folder")
    p.set_defaults(func=cmd_selftest)
    p = commands.add_parser("benchmark", help="compare data-processing models")
    p.add_argument("--processors", required=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_benchmark)
    p = commands.add_parser("samples", help="make the test images")
    p.add_argument("folder")
    p.set_defaults(func=cmd_samples)
    args = parser.parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        sys.exit("\nStopped. Run the same command again to continue; finished work is kept.")


if __name__ == "__main__":
    main()
