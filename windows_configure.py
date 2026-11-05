#!/usr/bin/env python3
"""
windows_configure.py - writes the Hermes, Hindsight and document-tool settings on Windows,
and checks them. windows_setup.ps1 runs it for you; you can also run it yourself:

    py windows_configure.py configure      write all settings (keeps a backup of each file)
    py windows_configure.py check          check every setting (read-only)

Your choices (which model chats, looks at photos, saves memories, ...) are kept in
local_ai_choices.json in Hermes's folder. Change them with the settings window
(local_ai_settings.pyw); running the setup again keeps them.

Long-term memory is saved by the main model on the graphics card (gemma4:26b-128k), so no
second Ollama is needed.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import local_ai_rules as base  # noqa: E402  (shared texts: rules, skill, Hindsight settings)

# --------------------------------------------------------------------------
# Where things are on Windows
# --------------------------------------------------------------------------
LOCAL = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
HERMES_DIR = Path(os.environ.get("HERMES_HOME", LOCAL / "hermes"))
HERMES_CONFIG = HERMES_DIR / "config.yaml"
HERMES_ENV = HERMES_DIR / ".env"
HINDSIGHT_CONFIG = HERMES_DIR / "hindsight" / "config.json"
HINDSIGHT_PLUGIN = HERMES_DIR / "plugins" / "hindsight"
HINDSIGHT_PROFILE_ENV = Path.home() / ".hindsight" / "profiles" / "hermes.env"
HINDSIGHT_SERVER_LOG = Path.home() / ".hindsight" / "profiles" / "hermes.log"
HINDSIGHT_EMBED_LOG = HERMES_DIR / "logs" / "hindsight-embed.log"
SOUL_FILE = HERMES_DIR / "SOUL.md"
# The document tool's settings. Installs made before the rename keep their old file name.
SETTINGS_FILE = HERMES_DIR / "local_ai_docs.json"
if not SETTINGS_FILE.exists() and (HERMES_DIR / "locallinux.json").exists():
    SETTINGS_FILE = HERMES_DIR / "locallinux.json"
SKILL_FILE = HERMES_DIR / "skills" / "document-ocr" / "SKILL.md"
CHOICES_FILE = HERMES_DIR / "local_ai_choices.json"   # what the settings window saves
READY_FILE = HERMES_DIR / "local_ai_ready.json"       # written when the memory test passes
# Copies of these scripts. Installs made before the rename keep using their old folder.
APP_DIR = LOCAL / "local-ai-windows"
if not APP_DIR.exists() and (LOCAL / "locallinux").exists():
    APP_DIR = LOCAL / "locallinux"
# Goose, the second AI app: its own settings, rules (.goosehints) and memory
ROAMING = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
GOOSE_DIR = ROAMING / "Block" / "goose" / "config"
GOOSE_CONFIG = GOOSE_DIR / "config.yaml"
GOOSE_HINTS = GOOSE_DIR / ".goosehints"
GOOSE_APP_DIR = LOCAL / "Programs" / "Goose"
DOCS_SCRIPT = APP_DIR / "hermes_docs.py"

OLLAMA = "http://127.0.0.1:11434"   # not "localhost": on Windows that first tries IPv6 and waits 2 s
ENDPOINT = OLLAMA + "/v1"
# The downloaded models and the conversation length each one gets. Ollama's own default is too
# short (often 4K), so the setup makes a copy of each model with a longer length, named after the
# model and the length, for example gemma4:26b-128k. A copy uses no extra disk space, and the
# original model (gemma4:26b) stays as it is.
REQUIRED_MODELS = {"gemma4:26b": 131072, "nemotron-3.5-lightning": 65536, "qwen3-vl:8b": 32768,
                   "glm-ocr": 16384}
EXTRA_MODELS = {"gemma4:e4b": 131072, "qwen3.8": 65536}
CONTEXT_CHOICES = (16384, 32768, 65536, 131072, 262144)


def copy_name(model, ctx):
    """gemma4:26b with 131072 -> gemma4:26b-128k;  glm-ocr with 16384 -> glm-ocr:16k"""
    name, _, tag = model.partition(":")
    return f"{name}:{tag}-{ctx // 1024}k" if tag else f"{name}:{ctx // 1024}k"


COPIES = {copy_name(m, ctx): (m, ctx) for m, ctx in REQUIRED_MODELS.items()}
EXTRA_COPIES = {copy_name(m, ctx): (m, ctx) for m, ctx in EXTRA_MODELS.items()}
ALL_COPIES = {**COPIES, **EXTRA_COPIES}
COORDINATOR = copy_name("gemma4:26b", 131072)                 # chat, photos, memory, reviewer
PROCESSOR = copy_name("nemotron-3.5-lightning", 65536)        # side tasks, finding values
VISION = copy_name("qwen3-vl:8b", 32768)                      # second reading of photos
OCR = copy_name("glm-ocr", 16384)                             # reading the text in photos
# The names earlier versions of this setup used; they are renamed automatically.
OLD_NAMES = {"gemma4-hermes": COORDINATOR, "nemotron-helper": PROCESSOR, "qwen3-vl-hermes": VISION,
             "glm-ocr-hermes": OCR, "gemma4-e4b-hermes": copy_name("gemma4:e4b", 131072),
             "qwen3.8-hermes": copy_name("qwen3.8", 65536)}


def copy_info(name):
    """(original model, conversation length) of a copy such as gemma4:26b-64k, or None."""
    if name in ALL_COPIES:
        return ALL_COPIES[name]
    for model in {**REQUIRED_MODELS, **EXTRA_MODELS}:
        for ctx in CONTEXT_CHOICES:
            if copy_name(model, ctx).lower() == str(name).lower():
                return model, ctx
    return None
# Hermes looks at photos in a chat with the main model itself: it can read images, and using it
# avoids swapping models on the graphics card, which made each photo answer take minutes.
CHAT_VISION = COORDINATOR
OLLAMA_ENV = {"OLLAMA_KEEP_ALIVE": "30m", "OLLAMA_MAX_LOADED_MODELS": "1", "OLLAMA_NUM_PARALLEL": "1",
              "OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_GPU_OVERHEAD": str(1024 ** 3),
              "GGML_CUDA_DISABLE_GRAPHS": "1"}
NO_WINDOW = 0x08000000 if os.name == "nt" else 0     # no black console window from the settings app

# The recommended choices. "same" means "the same model as the chat model".
DEFAULT_CHOICES = {
    "chat_model": COORDINATOR,      # the model you talk to
    "chat_context": "default",      # conversation length in tokens, or "default" for the model's own
    "photo_model": "same",          # looks at photos in a chat
    "helper_model": PROCESSOR,      # side tasks Hermes hands off
    "memory_model": "same",         # reads chats and saves long-term memories
    "docs_profile": "balanced",     # document tool: "balanced" (tested faster, 12 of 12 values) or "quality"
    "docs_ocr": OCR,                # document tool: reads the text
    "docs_vision": VISION,          # document tool: second reading, tables
    "docs_processor": COORDINATOR,  # document tool: finds the values (gemma4 beat nemotron in the test,
                                    # and it is already loaded for chat and review: no model swap)
    "docs_reviewer": COORDINATOR,   # document tool: looks again at doubtful values
    "keep_alive": "30m",            # how long an unused model stays on the graphics card
    "gpu_free_mb": 1024,            # graphics card memory kept free for photos and the desktop
    "offline": "on",                # "on": nothing is downloaded once the memory test has passed
    "goose_model": "same",          # Goose's chat model ("same" = the Hermes chat model)
    "goose_mode": "smart_approve",  # when Goose asks before it acts
}

# Offline mode: the memory server (started with uvx) and its two search models (from Hugging
# Face) use only the copies already saved on this PC, and Hermes stops checking for updates.
OFFLINE_ENV = {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "UV_OFFLINE": "1"}
# Always set for the memory server: no traces sent anywhere, and LiteLLM (a library it includes) uses
# its built-in price list instead of downloading one.
PRIVATE_SERVER_SETTINGS = {"HINDSIGHT_API_OTEL_TRACES_ENABLED": "false", "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                           "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}


def load_choices():
    choices = dict(DEFAULT_CHOICES)
    try:
        saved = json.loads(CHOICES_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            choices.update({k: OLD_NAMES.get(v, v) if isinstance(v, str) else v for k, v in saved.items()
                            if k in DEFAULT_CHOICES and v not in (None, "")})
    except (OSError, ValueError):
        pass
    return choices


def save_choices(choices):
    CHOICES_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {k: choices.get(k, v) for k, v in DEFAULT_CHOICES.items()}
    CHOICES_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def chat_model(c):
    """The chat model's copy with the chosen conversation length, e.g. gemma4:26b-64k."""
    info = copy_info(c["chat_model"])
    return copy_name(info[0], chat_context(c)) if info else c["chat_model"]


def photo_model(c):
    return chat_model(c) if c["photo_model"] == "same" else c["photo_model"]


def memory_model(c):
    return chat_model(c) if c["memory_model"] == "same" else c["memory_model"]


def goose_model(c):
    return chat_model(c) if c.get("goose_model", "same") == "same" else c["goose_model"]


def chat_context(c):
    """The conversation length for the chat model: the chosen one, else the model copy's own."""
    if str(c.get("chat_context")).isdigit():
        return int(c["chat_context"])
    return (copy_info(c["chat_model"]) or (None, 65536))[1]


def uv_cache_dirs():
    if os.environ.get("UV_CACHE_DIR"):
        return [Path(os.environ["UV_CACHE_DIR"])]
    return [LOCAL / "uv" / "cache", Path.home() / ".cache" / "uv"]


def huggingface_hub_dir():
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    home = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
    return home / "hub"


def offline_ready():
    """Whether everything the memory server downloads on its first start is saved on this PC.
    Returns (ready, explanation)."""
    missing = []
    server = any(any(cache.glob(pattern)) for cache in uv_cache_dirs() if cache.is_dir()
                 for pattern in ("archive-v0/*/Lib/site-packages/hindsight_api",
                                 "archive-v0/*/lib/python*/site-packages/hindsight_api"))
    if not server:
        missing.append("the memory server program")
    hub = huggingface_hub_dir()
    models = [d.name for d in hub.glob("models--*")] if hub.is_dir() else []
    if len(models) < 2:
        missing.append("the memory search models")
    if not READY_FILE.exists():
        missing.append("a passing memory test")
    if missing:
        return False, "still needs " + ", ".join(missing)
    return True, "everything is saved on this PC"


def offline_active(c):
    return c.get("offline") == "on" and offline_ready()[0]


def after_memory_test_passed():
    """Called by memory_quick_test.py when memory works: turn on Offline mode if it's chosen."""
    READY_FILE.parent.mkdir(parents=True, exist_ok=True)
    READY_FILE.write_text(json.dumps({"memory_test_passed": datetime.datetime.now().isoformat(
        timespec="seconds")}) + "\n", encoding="utf-8")
    c = load_choices()
    if c.get("offline") != "on":
        return
    ready, why = offline_ready()
    if not ready:
        warn(f"Offline mode isn't on yet: it {why}.")
        return
    set_offline_settings(c)
    write_hermes_config(c)
    ok("Offline mode is on: nothing is downloaded from now on. Close Hermes and open it again.")


def get_user_variable(key):
    """A user environment variable as saved for new programs (on Windows, read from the registry)."""
    if os.name != "nt":
        return os.environ.get(key)
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            return str(winreg.QueryValueEx(handle, key)[0])
    except OSError:
        return None


def set_user_variable(key, value):
    """Set (or with value None remove) a user environment variable, here and for new programs."""
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    if os.name != "nt":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as handle:
        if value is None:
            try:
                winreg.DeleteValue(handle, key)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(handle, key, 0, winreg.REG_SZ, value)


def tell_windows_variables_changed():
    if os.name == "nt":
        import ctypes
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000, None)


def set_offline_settings(c):
    """Turn Offline mode on or off. Returns True when something changed."""
    active = offline_active(c)
    changed = False
    for key, value in OFFLINE_ENV.items():
        want = value if active else None
        if get_user_variable(key) != want:
            changed = True
            set_user_variable(key, want)
    if changed:
        tell_windows_variables_changed()
    if active:
        ok("Offline mode is on: the memory server and its search models use only the copies on this PC.")
    elif c.get("offline") == "on":
        warn(f"Offline mode turns on by itself when the memory test passes (it {offline_ready()[1]}).")
    else:
        ok("Offline mode is off: the memory server may download updates.")
    return changed


def ollama_env(c):
    env = dict(OLLAMA_ENV)
    env["OLLAMA_KEEP_ALIVE"] = str(c["keep_alive"])
    env["OLLAMA_GPU_OVERHEAD"] = str(int(c["gpu_free_mb"]) * 1024 * 1024)
    return env


def say(text=""):
    print(text, flush=True)


def ok(text):
    say(f"  [OK] {text}")


def warn(text):
    say(f"  [!]  {text}")


def backup(path, keep=5):
    """Copy a settings file before it changes; only the newest few copies are kept."""
    if path.exists():
        target = path.with_name(f"{path.name}.backup-{datetime.datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(path, target)
        for old in sorted(path.parent.glob(path.name + ".backup-*"))[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass
        return target
    return None


def yaml():
    try:
        import yaml as module
        return module
    except ImportError:
        sys.exit("Python's yaml module is missing. Install it with: py -m pip install --user pyyaml")


def find_hermes():
    return shutil.which("hermes")


def python_for_docs():
    return Path(sys.executable)


def docs_command():
    """The hermes-docs command as Hermes's terminal (Git Bash) runs it."""
    to_bash = lambda p: str(p).replace("\\", "/")
    return f'"{to_bash(python_for_docs())}" "{to_bash(DOCS_SCRIPT)}"'


# --------------------------------------------------------------------------
# The names memory_quick_test.py uses
# --------------------------------------------------------------------------
def load_settings():
    return {"memory_on_cpu": False}


def memory_target(settings=None):
    return memory_model(load_choices()), ENDPOINT


def ensure_uvx():
    found = shutil.which("uvx")
    if found:
        ok(f"uvx (starts Hindsight's memory server) is available: {found}")
    else:
        warn("uvx was not found. Install uv with:  winget install astral-sh.uv  "
             "then open a new PowerShell window.")
    return found


def configure_hindsight_server(model, endpoint):
    values = {**base.HINDSIGHT_SERVER_SETTINGS, **PRIVATE_SERVER_SETTINGS}
    if not HINDSIGHT_PROFILE_ENV.exists():
        values = {"HINDSIGHT_API_LLM_PROVIDER": "openai", "HINDSIGHT_API_LLM_API_KEY": "ollama",
                  "HINDSIGHT_API_LLM_MODEL": model, "HINDSIGHT_API_LLM_BASE_URL": endpoint, **values}
    else:   # memory is saved by the main model on the graphics card on Windows
        values.update({"HINDSIGHT_API_LLM_MODEL": model, "HINDSIGHT_API_LLM_BASE_URL": endpoint})
    HINDSIGHT_PROFILE_ENV.parent.mkdir(parents=True, exist_ok=True)
    base.set_profile_settings(HINDSIGHT_PROFILE_ENV, values)
    base.update_env_file(HERMES_ENV, {**base.HINDSIGHT_SERVER_SETTINGS,
                                      "HINDSIGHT_LLM_API_KEY": "ollama",
                                      "HINDSIGHT_API_LLM_BASE_URL": endpoint})
    ok("Hindsight's memory server: 15-minute timeout, one request at a time, search models on the CPU, "
       "no traces or usage data sent.")


# --------------------------------------------------------------------------
# Clearing memories
# --------------------------------------------------------------------------
MEMORY_BACKUPS = APP_DIR / "memory-backups"
GOOSE_DATA = ROAMING / "Block" / "goose" / "data"


def memory_bank():
    try:
        return json.loads(HINDSIGHT_CONFIG.read_text(encoding="utf-8")).get("bank_id") or "hermes"
    except (OSError, ValueError):
        return "hermes"


def goose_memory_dirs():
    return [d for d in (GOOSE_DIR / "memory", GOOSE_DATA / "memory") if d.is_dir()]


def memory_parts():
    """What can be cleared, for the settings window: key -> (title, explanation)."""
    return {
        "hindsight": ("Hermes's long-term memory", "Every fact Hindsight saved from your Hermes chats."),
        "hermes_notes": ("Hermes's own notes", "The short notes Hermes keeps about you (its memories folder)."),
        "goose": ("Goose's memories", "What Goose saved with its Memory extension."),
        "goose_chats": ("Goose's chat history", "All Goose chats. Goose also finds facts in earlier chats "
                        "(its chat recall), so clear this too for a fresh start."),
        "rules": ("Rules", "Empties \"My rules\" and \"Rule changes\" in SOUL.md (Hermes) and .goosehints (Goose)."),
    }


def http_call(method, url, body=None, timeout=120):
    request = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")[:300]


def memory_server_running(port):
    import socket
    with socket.socket() as sock:
        sock.settimeout(2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_memory_server():
    """Start Hindsight's memory server with one short Hermes chat that goes to a helper memory."""
    port = memory_server_port()
    if memory_server_running(port):
        return port
    import memory_quick_test as test
    test.setup = sys.modules[__name__]
    if not test.start_server(find_hermes(), port, "clear-helper"):
        return None
    http_call("DELETE", f"http://127.0.0.1:{port}/v1/default/banks/clear-helper")
    return port


def wait_for_bank(base_url, bank, minutes=10):
    for _ in range(minutes * 12):
        status, banks = http_call("GET", base_url + "/v1/default/banks")
        names = json.dumps(banks)
        if status == 200 and f'"{bank}"' in names:
            status, stats = http_call("GET", f"{base_url}/v1/default/banks/{bank}/stats")
            if status == 200:
                return True
        time.sleep(5)
    return False


def wait_for_operation(base_url, banks, answer, minutes=20):
    """Wait until an async operation (such as a clone) has finished. True when it completed."""
    op = answer.get("operation_id") or answer.get("id") if isinstance(answer, dict) else None
    if not op:
        return True    # finished right away
    seen = False
    for attempt in range(minutes * 12):
        if attempt == 6 and not seen:
            return True    # this server version doesn't report the operation; the bank check follows
        for bank in banks:
            status, info = http_call("GET", f"{base_url}/v1/default/banks/{bank}/operations/{op}")
            if status == 200 and isinstance(info, dict):
                seen = True
                state = str(info.get("status", "")).lower()
                if state in ("completed", "succeeded", "done", "success"):
                    return True
                if state in ("failed", "cancelled", "canceled", "error"):
                    return False
                break
        time.sleep(5)
    return False


def move_out(path, backup_dir, keep_backup):
    """Move a file or folder into the backup, or delete it."""
    if not path.exists():
        return False
    if keep_backup:
        target = backup_dir / path.name
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
    elif path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def empty_rules(path, backup_dir, keep_backup):
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if keep_backup:
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup_dir / path.name)
    found = re.search(r"^## My rules[ \t]*$", text, re.M)   # the heading, not a mention in a sentence
    if not found:
        return False
    path.write_text(text[:found.start()] + "## My rules\n<!-- Rules for every task. Added when I correct the assistant; "
                    "I can edit them too. -->\n\n## Rule changes\n<!-- One dated line per change: what changed "
                    "and why. -->\n", encoding="utf-8")
    return True


def clear_memories(parts, keep_backup=True):
    """Clear the chosen memories. parts: keys from memory_parts(). Returns True when all worked."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = MEMORY_BACKUPS / stamp
    all_ok = True
    if "hindsight" in parts:
        say("Hermes's long-term memory (Hindsight):")
        port = start_memory_server()
        if not port:
            warn("The memory server didn't start, so nothing was cleared. Open Hermes, send one message, try again.")
            all_ok = False
        else:
            base_url, bank = f"http://127.0.0.1:{port}", memory_bank()
            if keep_backup:
                copy = f"{bank}-backup-{stamp}"
                status, answer = http_call(
                    "POST", f"{base_url}/v1/default/banks/{bank}/clone?target_bank_id={copy}&include_data=true"
                    "&include_bank_config=true&include_history=false", timeout=600)
                if status in (200, 201, 202) and not wait_for_operation(base_url, (bank, copy), answer):
                    status = "the copy didn't finish"
                if status not in (200, 201, 202) or not wait_for_bank(base_url, copy):
                    warn(f"Couldn't make the backup ({status}: {str(answer)[:150]}), so nothing was cleared.")
                    all_ok = False
                    parts = [p for p in parts if p != "hindsight"]
                else:
                    ok(f"Backup made: the memory '{copy}' (it stays in the memory database)")
            if "hindsight" in parts:
                status, answer = http_call("DELETE", f"{base_url}/v1/default/banks/{bank}", timeout=300)
                if status in (200, 202, 204, 404):
                    ok("Cleared. Hermes starts a new, empty long-term memory with your next chat.")
                else:
                    warn(f"Couldn't clear it ({status}: {str(answer)[:150]})")
                    all_ok = False
    if "hermes_notes" in parts:
        notes = HERMES_DIR / "memories"
        files = list(notes.iterdir()) if notes.is_dir() else []
        for item in files:
            move_out(item, backup_dir / "hermes-memories", keep_backup)
        ok(f"Hermes's own notes: {len(files)} file(s) cleared" if files else "Hermes's own notes: already empty")
    if "goose" in parts:
        dirs = goose_memory_dirs()
        for d in dirs:
            move_out(d, backup_dir / "goose", keep_backup)
        ok("Goose's memories cleared" if dirs else "Goose's memories: none saved yet")
    if "goose_chats" in parts:
        sessions = GOOSE_DATA / "sessions"
        try:
            ok("Goose's chat history cleared" if move_out(sessions, backup_dir / "goose", keep_backup)
               else "Goose's chat history: none yet")
        except OSError as error:
            warn(f"Couldn't clear Goose's chat history ({error}). Close Goose completely and try again.")
            all_ok = False
    if "rules" in parts:
        for path, name in ((SOUL_FILE, "Hermes (SOUL.md)"), (GOOSE_HINTS, "Goose (.goosehints)")):
            if empty_rules(path, backup_dir / "rules", keep_backup):
                ok(f"Rules emptied: {name}")
    if keep_backup and backup_dir.exists():
        ok(f"Backup of the files: {backup_dir}")
    return all_ok


# --------------------------------------------------------------------------
# configure
# --------------------------------------------------------------------------
def write_hermes_config(c=None):
    c = c or load_choices()
    y = yaml()
    config = {}
    if HERMES_CONFIG.exists():
        config = y.safe_load(HERMES_CONFIG.read_text(encoding="utf-8")) or {}
        saved = backup(HERMES_CONFIG)
        ok(f"Backed up Hermes's settings to {saved}")
    sec = base.section
    sec(config, "model").update({"default": chat_model(c), "provider": "custom", "base_url": ENDPOINT,
                                 "api_key": "no-key", "context_length": chat_context(c)})
    sec(sec(config, "auxiliary"), "vision").update({"provider": "main", "model": photo_model(c),
                                                    "base_url": ENDPOINT, "api_key": "no-key"})
    sec(config, "delegation").update({"model": c["helper_model"], "base_url": ENDPOINT,
                                      "api_key": "no-key", "max_concurrent_children": 1})
    sec(config, "updates")["check"] = not offline_active(c)   # no update checks in Offline mode
    terminal = sec(config, "terminal")
    terminal["timeout"] = max(int(terminal.get("timeout") or 0), 1800)
    sec(config, "memory")["provider"] = "hindsight"
    if config.get("max_concurrent_sessions", 0) is None:   # an empty section Hermes warns about
        config.pop("max_concurrent_sessions")
    HERMES_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    HERMES_CONFIG.write_text(y.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    ok(f"Hermes settings written: chat {chat_model(c)} ({chat_context(c) // 1024}K), photos "
       f"{photo_model(c)}, helper {c['helper_model']}")


def write_hindsight_config(c=None):
    c = c or load_choices()
    model = memory_model(c)
    data = {}
    if HINDSIGHT_CONFIG.exists():
        try:
            data = json.loads(HINDSIGHT_CONFIG.read_text(encoding="utf-8")) or {}
        except ValueError:
            data = {}
        backup(HINDSIGHT_CONFIG)
    data.update({"mode": "local_embedded", "llm_provider": "openai_compatible",
                 "llm_model": model, "llm_base_url": ENDPOINT})
    data.pop("bank_id", None) if data.get("bank_id", "").startswith(("setup-test", "quick-test")) else None
    HINDSIGHT_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    HINDSIGHT_CONFIG.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    ok(f"Hindsight settings written (memories saved by {model} on the graphics card)")
    configure_hindsight_server(model, ENDPOINT)


def write_rules():
    linux_path = "~/.hermes/SOUL.md"        # the shared rules text names the Linux location of this file
    rules = base.SOUL_RULES.replace(linux_path, str(SOUL_FILE))
    text = SOUL_FILE.read_text(encoding="utf-8") if SOUL_FILE.exists() else ""
    if "## Rule changes" in text:
        if linux_path in text:
            backup(SOUL_FILE)
            SOUL_FILE.write_text(text.replace(linux_path, str(SOUL_FILE)), encoding="utf-8")
            ok(f"SOUL.md now names its Windows location ({SOUL_FILE}), so new rules are saved there.")
        else:
            ok("SOUL.md already has the rules sections.")
        return
    backup(SOUL_FILE)
    if base.OLD_SOUL_RULES.strip() in text:
        text = text.replace(base.OLD_SOUL_RULES.strip(), rules.strip())
    else:
        text = (text.rstrip() + "\n\n" if text.strip() else "") + rules
    SOUL_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOUL_FILE.write_text(text, encoding="utf-8")
    ok("Rules added to SOUL.md: Hermes keeps 'My rules' and notes every change under 'Rule changes'.")


def write_docs_settings(c=None):
    c = c or load_choices()
    base_of = lambda name: (copy_info(name) or (name,))[0]
    data = {"profile": c["docs_profile"], "coordinator": base_of(chat_model(c)),
            "processor": base_of(c["docs_processor"]), "reviewer": base_of(c["docs_reviewer"]),
            "memory_on_cpu": False,
            "context": {"coordinator": chat_context(c),
                        "processor": (copy_info(c["docs_processor"]) or (None, 65536))[1]},
            "docs": {"ollama": OLLAMA, "profile": c["docs_profile"],
                     "models": {"ocr": c["docs_ocr"], "vision": c["docs_vision"],
                                "processor": c["docs_processor"], "reviewer": c["docs_reviewer"]}}}
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    SKILL_FILE.parent.mkdir(parents=True, exist_ok=True)
    skill = base.SKILL_TEXT.replace("`{DOCS_TOOL}", "`" + docs_command()).replace(
        "{DOCS_TOOL}", docs_command()).replace("{FIELDS_FILE}", str(HERMES_DIR / "docs" / "fields.json"))
    SKILL_FILE.write_text(skill, encoding="utf-8")
    ok(f"Document tool settings and the document-ocr skill written ({SKILL_FILE})")


# --------------------------------------------------------------------------
# Goose
# --------------------------------------------------------------------------
GOOSE_RULES = """\
<!-- Written by the Local AI setup (windows_configure.py). You can edit this file. -->
You are goose, running fully on this PC through Ollama. These are my rules for every session.

## How to keep my rules
- Before you start a task, read "## My rules" below and follow them.
- When I correct you and the correction should apply to every task, add it yourself as a new line under
  "## My rules" in this file ({hints}) with your file editing tool, then tell me in one line what you added.
- Never remove or weaken a rule unless I ask. If a new correction conflicts with a rule, ask me which one to keep.
- Every time you add or change a rule, add one dated line under "## Rule changes": what changed, and what
  went wrong before that made the change necessary.
- New rules apply from the next session; follow them in this session too.

## Memory
- When I tell you a lasting fact about me or my work, or ask you to remember something, save it as a
  global memory with the memory extension, then say in one line what you saved.
- Before you answer a question about me or my earlier work, check your memories.

## Photos, scans and PDFs
- For exact text, numbers, totals or dates in photos, scans or PDFs, don't guess: use the document tool
  in the shell. It runs on this PC and checks every value against the page.
  - `hermes-docs process "<file or folder>"` (about 1-2 minutes per photo; for more than about 20 files
    add `--background` and check with `hermes-docs status` every few minutes).
  - It writes a `hermes_docs` folder next to the originals with one Markdown and one JSON file per document.
    Every value has a page, an exact quote and a status. Treat only verified values as facts; report
    disputed, unverified and not_found_on_page values as not confirmed. Never fill in a missing value.
  - `hermes-docs search "words"` finds documents processed before; `hermes-docs show "<file>" --page N`
    shows one page's text.
  - If `hermes-docs` isn't found, use `"{docs_cmd}"` instead.

## My rules
<!-- Rules for every session. Goose adds them here when I correct it; I can edit them too. -->

## Rule changes
<!-- One dated line per change: what changed and why. -->
"""


def find_goose():
    """Goose.exe of the Goose desktop app, or None."""
    if GOOSE_APP_DIR.is_dir():
        for found in sorted(GOOSE_APP_DIR.rglob("Goose.exe")):
            return found
    return None


def write_goose_config(c=None):
    """Point Goose at Ollama with the chosen model, and turn on its developer and memory extensions."""
    c = c or load_choices()
    y = yaml()
    config = {}
    if GOOSE_CONFIG.exists():
        config = y.safe_load(GOOSE_CONFIG.read_text(encoding="utf-8")) or {}
        backup(GOOSE_CONFIG)
    model = goose_model(c)
    ctx = (copy_info(model) or (None, 65536))[1]
    config.update({"GOOSE_PROVIDER": "ollama", "GOOSE_MODEL": model, "OLLAMA_HOST": OLLAMA,
                   "GOOSE_INPUT_LIMIT": ctx, "GOOSE_MODE": c.get("goose_mode", "smart_approve"),
                   "GOOSE_TELEMETRY_ENABLED": False, "active_provider": "ollama"})
    providers = base.section(config, "providers")
    base.section(providers, "ollama").update({"enabled": True, "model": model, "configured": True})
    extensions = base.section(config, "extensions")
    for name in ("developer", "memory"):
        entry = base.section(extensions, name)
        entry.update({"type": "builtin", "name": name, "enabled": True, "bundled": True})
        entry.setdefault("timeout", 300)
    GOOSE_DIR.mkdir(parents=True, exist_ok=True)
    GOOSE_CONFIG.write_text(y.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    ok(f"Goose settings written: Ollama, {model} ({ctx // 1024}K), developer and memory extensions on")


def write_goose_rules():
    rules = GOOSE_RULES.format(hints=GOOSE_HINTS, docs_cmd=APP_DIR / "hermes-docs.cmd")
    if GOOSE_HINTS.exists():
        text = GOOSE_HINTS.read_text(encoding="utf-8")
        if "## Rule changes" in text:
            ok("Goose's rules file (.goosehints) is already there; your rules are kept.")
            return
        backup(GOOSE_HINTS)
        rules = text.rstrip() + "\n\n" + rules
    GOOSE_DIR.mkdir(parents=True, exist_ok=True)
    GOOSE_HINTS.write_text(rules, encoding="utf-8")
    ok(f"Goose's rules written: {GOOSE_HINTS} (its own 'My rules' and 'Rule changes')")


def configure_goose(c=None):
    if not find_goose() and not GOOSE_CONFIG.exists():
        return False
    write_goose_config(c)
    write_goose_rules()
    return True


# --------------------------------------------------------------------------
# Ollama: model copies, settings, restart
# --------------------------------------------------------------------------
def find_ollama():
    found = shutil.which("ollama")
    if found:
        return found
    default = LOCAL / "Programs" / "Ollama" / "ollama.exe"
    return str(default) if default.exists() else None


def installed_models():
    """Names of the downloaded models (without ':latest'), or None when Ollama doesn't answer."""
    try:
        return {m["name"].split(":latest")[0] for m in http_json(OLLAMA + "/api/tags", timeout=5)["models"]}
    except Exception:
        return None


def model_info(name):
    try:
        return http_json(OLLAMA + "/api/show", {"model": name}, timeout=20)
    except Exception:
        return {}


def can_see_images(name):
    capabilities = model_info(name).get("capabilities")
    return None if capabilities is None else "vision" in capabilities


def context_of(name):
    found = re.search(r"num_ctx\s+(\d+)", model_info(name).get("parameters") or "")
    return int(found.group(1)) if found else None


def make_copy(name, ctx):
    """(Re)create a long-context copy such as gemma4:26b-128k with the given conversation length."""
    source = (copy_info(name) or (None,))[0]
    ollama = find_ollama()
    if not source or not ollama:
        warn(f"Can't make {name} (unknown model or Ollama not found).")
        return False
    # Model names can hold / and :, which Windows doesn't allow in file names.
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    modelfile = Path(os.environ.get("TEMP", "/tmp")) / f"Modelfile-{safe}"
    try:
        modelfile.write_text(f"FROM {source}\nPARAMETER num_ctx {ctx}\n", encoding="ascii")
        done = subprocess.run([ollama, "create", name, "-f", str(modelfile)], capture_output=True, text=True,
                              creationflags=NO_WINDOW)
    except OSError as error:
        warn(f"Couldn't make {name}: {error}")
        return False
    finally:
        modelfile.unlink(missing_ok=True)
    if done.returncode == 0:
        ok(f"{name} = {source} with {ctx // 1024}K conversation length")
        return True
    warn(f"Couldn't make {name}: {(done.stderr or done.stdout).strip()[:200]}")
    return False


def used_models(c):
    return {chat_model(c), photo_model(c), memory_model(c), c["helper_model"], c["docs_ocr"],
            c["docs_vision"], c["docs_processor"], c["docs_reviewer"], goose_model(c)}


def wanted_copies(c, installed):
    """The copies to have: the four the setup needs, the extra ones whose model is downloaded, and
    every copy a setting uses (for example gemma4:26b-64k for a shorter conversation length)."""
    names = dict(COPIES)
    names.update({n: v for n, v in EXTRA_COPIES.items() if v[0] in installed})
    names.update({n: copy_info(n) for n in used_models(c) if copy_info(n)})
    return names


def ensure_copies(c):
    """Make the missing copies (or fix their length), then remove copies with the old -hermes names."""
    installed = installed_models()
    if installed is None:
        warn("Ollama doesn't answer, so the model copies weren't checked. Start Ollama and save again.")
        return False
    for name, (source, ctx) in wanted_copies(c, installed).items():
        if source not in installed:
            if name in used_models(c):
                warn(f"{name}: {source} isn't downloaded yet. Download it with: ollama pull {source}")
            continue
        if name in installed and context_of(name) == ctx:
            continue
        make_copy(name, ctx)
    return remove_old_copies(installed)


def remove_old_copies(installed):
    """Remove the copies named like gemma4-hermes (the originals and their files are kept)."""
    ollama, removed = find_ollama(), False
    for old, new in OLD_NAMES.items():
        if old in installed and new in (installed_models() or set()) and ollama:
            done = subprocess.run([ollama, "rm", old], capture_output=True, text=True, creationflags=NO_WINDOW)
            if done.returncode == 0:
                ok(f"Removed the old name {old} (now {new})")
                removed = True
    return removed


def set_ollama_settings(c):
    """Save the Ollama settings as user environment variables. Returns True when something changed."""
    changed = False
    for key, value in ollama_env(c).items():
        if get_user_variable(key) != value:
            changed = True
        set_user_variable(key, value)
    if changed:   # tell Windows (and new programs) that the variables changed
        tell_windows_variables_changed()
    return changed


def start_ollama():
    """Start Ollama if it isn't running (for example right after Windows starts). True when it answers."""
    if installed_models() is not None:
        return True
    say("Starting Ollama...")
    app = LOCAL / "Programs" / "Ollama" / "ollama app.exe"
    if os.name == "nt" and app.exists():
        subprocess.Popen([str(app)], creationflags=NO_WINDOW)
    elif find_ollama():
        subprocess.Popen([find_ollama(), "serve"], creationflags=NO_WINDOW)
    for _ in range(60):
        if installed_models() is not None:
            ok("Ollama is running")
            return True
        time.sleep(1)
    warn("Ollama didn't start. Open 'Ollama' from the Start menu.")
    return False


def stop_ollama_runners():
    """Stop Ollama's model runners (llama-server.exe in Ollama's folder) that are left over after
    Ollama itself stopped. They keep a model on the graphics card until they are stopped."""
    if os.name != "nt":
        return 0
    folder = str(LOCAL / "Programs" / "Ollama")
    command = ("$n = 0; Get-Process llama-server -ErrorAction SilentlyContinue | "
               f"Where-Object {{ $_.Path -like '{folder}\\*' }} | "
               "ForEach-Object { Stop-Process -Id $_.Id -Force; $n++ }; $n")
    done = subprocess.run(["powershell", "-NoProfile", "-Command", command], capture_output=True, text=True,
                          creationflags=NO_WINDOW)
    try:
        return int((done.stdout or "0").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 0


def free_graphics_card():
    """Unload every model from the graphics card, and stop runners Ollama left behind."""
    unloaded = 0
    try:
        loaded = http_json(OLLAMA + "/api/ps", timeout=5).get("models", [])
    except Exception:
        loaded = None
    for model in loaded or []:
        try:
            http_json(OLLAMA + "/api/generate", {"model": model["name"], "keep_alive": 0}, timeout=60)
            unloaded += 1
        except Exception as error:
            warn(f"Couldn't unload {model['name']}: {error}")
    if loaded is not None:
        ok(f"Unloaded {unloaded} model(s); Ollama keeps running")
    time.sleep(2)
    left = stop_ollama_runners()
    if left:
        ok(f"Stopped {left} model runner(s) left over from an earlier Ollama")
    if loaded is None and not left:
        say("  Nothing to free: Ollama isn't running and no runners were left.")
    return True


def restart_ollama():
    say("Restarting Ollama so it uses the new settings...")
    if os.name == "nt":
        for image in ("ollama app.exe", "ollama.exe"):
            subprocess.run(["taskkill", "/IM", image, "/F", "/T"], capture_output=True, creationflags=NO_WINDOW)
        stop_ollama_runners()
        app = LOCAL / "Programs" / "Ollama" / "ollama app.exe"
        time.sleep(2)
        if app.exists():
            subprocess.Popen([str(app)], creationflags=NO_WINDOW)
        elif find_ollama():
            subprocess.Popen([find_ollama(), "serve"], creationflags=NO_WINDOW)
    for _ in range(60):
        if installed_models() is not None:
            ok("Ollama is running again")
            return True
        time.sleep(1)
    warn("Ollama didn't come back. Start 'Ollama' from the Start menu.")
    return False


def memory_server_port():
    try:
        for line in HINDSIGHT_PROFILE_ENV.read_text(encoding="utf-8").splitlines():
            if line.startswith("HINDSIGHT_API_PORT="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return 9177


def process_command_line(pid):
    """The command line of a running process (Windows), or "" when it can't be read."""
    done = subprocess.run(["powershell", "-NoProfile", "-Command",
                           f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                          capture_output=True, text=True, errors="replace", creationflags=NO_WINDOW)
    return (done.stdout or "").strip()


def stop_memory_server():
    """Stop Hindsight's memory server; Hermes starts it again (with the new settings) in the next chat.
    Only a process that is Hindsight's server is stopped: another program on the same port is left alone."""
    if os.name != "nt":
        return False
    port = memory_server_port()
    listing = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
    pids = {line.split()[-1] for line in listing.splitlines()
            if re.search(rf"[:.]{port}\s", line) and "LISTENING" in line}
    stopped = False
    for pid in pids:
        if not pid.isdigit():
            continue
        command = process_command_line(pid)
        if "hindsight" not in command.lower():
            warn(f"Port {port} is used by another program (process {pid}: {command[:120] or 'unknown'}). "
                 "It was not stopped. Close Hermes and open it again instead.")
            continue
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        stopped = True
    if stopped:
        ok("Stopped the memory server. It starts again with the new model in your next Hermes chat.")
    return stopped


def apply_choices(c, restart=True):
    """Write every settings file from the choices. Returns what needs restarting."""
    before = load_choices()
    save_choices(c)
    say("Writing the settings")
    write_hermes_config(c)
    write_hindsight_config(c)
    write_rules()
    write_docs_settings(c)
    configure_goose(c)
    renamed = ensure_copies(c)
    if renamed and stop_memory_server():
        ok("The memory server will use the new model names from your next Hermes chat.")
    if not HINDSIGHT_PLUGIN.is_dir():
        warn("The Hindsight plugin isn't installed yet. Install it with: hermes plugins install hindsight")
    set_offline_settings(c)
    ollama_changed = set_ollama_settings(c)
    if ollama_changed:
        ok("Ollama settings saved: unused models leave the graphics card after "
           f"{c['keep_alive']}, {int(c['gpu_free_mb'])} MB kept free")
        if restart:
            restart_ollama()
    return {"ollama": ollama_changed, "memory": memory_model(before) != memory_model(c)}


def cmd_copies(args):
    say("Making the long-context copies (named after the model and its length, e.g. " + COORDINATOR + ")")
    if ensure_copies(load_choices()) and stop_memory_server():
        ok("The memory server will use the new model names from your next Hermes chat.")
    installed = installed_models() or set()
    for name in sorted(wanted_copies(load_choices(), installed)):
        if name in installed:
            ok(f"{name} ({context_of(name) // 1024 if context_of(name) else '?'}K)")
    return 0


def cmd_configure(args):
    apply_choices(load_choices(), restart=False)
    say("Done. Restart the Hermes app so it reads the new settings.")
    return 0


# --------------------------------------------------------------------------
# check (read-only)
# --------------------------------------------------------------------------
RESULTS = []


def add(passed, name, detail="", fix=""):
    RESULTS.append(passed)
    say(f"  [{'PASS' if passed else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    if not passed and fix:
        say(f"         What to do: {fix}")


def http_json(url, body=None, timeout=20):
    request = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def cmd_check(args):
    c = load_choices()
    say("Checking the Windows setup (read-only)")
    rerun = "Run windows_setup.ps1 again."
    code = subprocess.run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
                          capture_output=True, text=True) if shutil.which("nvidia-smi") else None
    add(bool(code and code.returncode == 0), "NVIDIA graphics card",
        code.stdout.strip() if code and code.returncode == 0 else "nvidia-smi not working",
        "Install or update the NVIDIA driver from nvidia.com.")
    for key, value in ollama_env(c).items():
        have = get_user_variable(key)
        add(have == value, f"Ollama setting {key}", str(have), rerun + " Then quit Ollama in the tray "
            "and start it again.")
    try:
        version = http_json(OLLAMA + "/api/version").get("version")
        add(True, "Ollama answers", f"version {version}")
        installed = {m["name"].split(":latest")[0] for m in http_json(OLLAMA + "/api/tags")["models"]}
        for name, (model, ctx) in wanted_copies(c, installed).items():
            if name not in installed:
                add(model not in installed and name not in used_models(c), f"{name} (from {model})",
                    "missing" if model in installed else f"{model} isn't downloaded", rerun)
                continue
            params = http_json(OLLAMA + "/api/show", {"model": name}).get("parameters") or ""
            found = re.search(r"num_ctx\s+(\d+)", params)
            add(bool(found and int(found.group(1)) == ctx), f"{name} (from {model})",
                f"{int(found.group(1)) // 1024}K context" if found else "no context size", rerun)
    except Exception as error:
        add(False, "Ollama answers", str(error), "Start Ollama from the Start menu, then check again.")
    hermes = find_hermes()
    add(bool(hermes), "Hermes program", hermes or "not found", rerun)
    try:
        config = yaml().safe_load(HERMES_CONFIG.read_text(encoding="utf-8")) or {}
    except OSError:
        config = {}
    for path, want in (("model.default", chat_model(c)), ("model.base_url", ENDPOINT),
                       ("model.context_length", chat_context(c)),
                       ("auxiliary.vision.model", photo_model(c)), ("delegation.model", c["helper_model"]),
                       ("memory.provider", "hindsight")):
        node = config
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        add(node == want, f"Hermes {path}", str(node), "py windows_configure.py configure")
    add(HINDSIGHT_PLUGIN.is_dir(), "Hindsight plugin", str(HINDSIGHT_PLUGIN),
        "hermes plugins install hindsight")
    add(bool(shutil.which("uvx")), "uvx (starts Hindsight's memory server)", shutil.which("uvx") or "missing",
        "winget install astral-sh.uv, then open a new PowerShell window")
    profile = HINDSIGHT_PROFILE_ENV.read_text(encoding="utf-8") if HINDSIGHT_PROFILE_ENV.exists() else ""
    for key, value in {**base.HINDSIGHT_SERVER_SETTINGS, "HINDSIGHT_API_LLM_MODEL": memory_model(c)}.items():
        add(re.search(rf"^{key}={re.escape(value)}\s*$", profile, re.M) is not None,
            f"Hindsight setting {key}", value, "py windows_configure.py configure")
    if HINDSIGHT_SERVER_LOG.exists():
        log = HINDSIGHT_SERVER_LOG.read_text(encoding="utf-8", errors="replace")
        recent = log[max(log.rfind("Uvicorn running"), 0):]
        failed = recent.count("Task execution failed")
        add(failed == 0, "Memory saves since the memory server started", f"{failed} failed",
            f"Read the errors near the end of {HINDSIGHT_SERVER_LOG}")
    soul = SOUL_FILE.read_text(encoding="utf-8") if SOUL_FILE.exists() else ""
    add("## Rule changes" in soul and "~/.hermes/SOUL.md" not in soul, "Rules in SOUL.md",
        "My rules + Rule changes, Windows location", "py windows_configure.py configure")
    sys.path.insert(0, str(APP_DIR))
    try:
        import hermes_docs
        for tool, package in (("tesseract", "UB-Mannheim.TesseractOCR"), ("pdftoppm", "oschwartz10612.Poppler")):
            found = hermes_docs.find_tool(tool)
            add(bool(found), f"Program '{tool}'", found or "missing", f"winget install {package}")
    except Exception as error:
        add(False, "Document tool", str(error), rerun)
    add(SKILL_FILE.exists(), "document-ocr skill", str(SKILL_FILE), "py windows_configure.py configure")
    goose = find_goose()
    if goose or GOOSE_CONFIG.exists():
        add(bool(goose), "Goose desktop app", str(goose or "not found"), "Run the setup again.")
        try:
            gconf = yaml().safe_load(GOOSE_CONFIG.read_text(encoding="utf-8")) or {}
        except OSError:
            gconf = {}
        add(gconf.get("GOOSE_PROVIDER") == "ollama" and gconf.get("GOOSE_MODEL") == goose_model(c),
            "Goose model", f"{gconf.get('GOOSE_PROVIDER')} / {gconf.get('GOOSE_MODEL')}",
            "Open Local AI Settings and click Save and apply.")
        ext = gconf.get("extensions") or {}
        add(all((ext.get(n) or {}).get("enabled") for n in ("developer", "memory")),
            "Goose extensions", "developer (shell, files) and memory", "Open Local AI Settings and click Save and apply.")
        hints = GOOSE_HINTS.read_text(encoding="utf-8") if GOOSE_HINTS.exists() else ""
        add("## Rule changes" in hints, "Goose's rules (.goosehints)", str(GOOSE_HINTS),
            "Open Local AI Settings and click Save and apply.")
    ready, why = offline_ready()
    if c.get("offline") == "on":
        active = all(get_user_variable(k) == v for k, v in OFFLINE_ENV.items())
        add(active or not ready, "Offline mode", "on" if active else f"turns on after the memory test ({why})",
            "Open Local AI Settings and click Save and apply.")
    else:
        add(not any(get_user_variable(k) for k in OFFLINE_ENV), "Offline mode", "off (you chose this)",
            "Open Local AI Settings and click Save and apply.")
    try:
        docs = json.loads(SETTINGS_FILE.read_text(encoding="utf-8")).get("docs", {})
    except (OSError, ValueError):
        docs = {}
    want = {"ocr": c["docs_ocr"], "vision": c["docs_vision"], "processor": c["docs_processor"],
            "reviewer": c["docs_reviewer"]}
    add(docs.get("models") == want and docs.get("profile") == c["docs_profile"], "Document tool models",
        ", ".join(f"{k} {v}" for k, v in want.items()) + f", {c['docs_profile']}",
        "py windows_configure.py configure")
    failed = RESULTS.count(False)
    say("")
    say(f"Done: {RESULTS.count(True)} PASS, {failed} FAIL.")
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(description="Hermes settings for Windows")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("configure").set_defaults(func=cmd_configure)
    commands.add_parser("check").set_defaults(func=cmd_check)
    commands.add_parser("copies", help="make the long-context model copies").set_defaults(func=cmd_copies)
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
