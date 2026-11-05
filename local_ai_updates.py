#!/usr/bin/env python3
"""
local_ai_updates.py - checks the Local AI programs and models for updates, and installs the ones
you pick. Local AI Settings uses it (Updates tab); you can also run it yourself:

    py local_ai_updates.py check        list what has a newer version (goes online, changes nothing)
    py local_ai_updates.py update ID..  update the listed items, for example: update ollama hermes

What it checks:
    programs  Ollama, Git, Tesseract, Poppler, uv (with winget), Hermes and its memory plugin,
              Goose, and the Python packages the scripts use
    models    your Ollama models (gemma4:26b, ...): it compares each one with the version on
              ollama.com. Updating a model downloads it again (6-25 GB) and remakes its
              long-context copies (gemma4:26b-128k, ...).

Nothing goes online unless you run "check" or "update". Updating Hermes pauses Offline mode,
because the memory server may need to download its new version: run the memory test once
(with the internet on) and Offline mode turns back on by itself.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import windows_configure as cfg  # noqa: E402

WINGET_PROGRAMS = {"Ollama.Ollama": "Ollama", "Git.Git": "Git for Windows",
                   "UB-Mannheim.TesseractOCR": "Tesseract", "oschwartz10612.Poppler": "Poppler",
                   "astral-sh.uv": "uv"}
PYTHON_PACKAGES = ("pyyaml", "pillow", "pillow-heif")
GOOSE_REPOS = ("aaif-goose/goose", "block/goose")
REGISTRY = "https://registry.ollama.ai/v2"
MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"


def say(text=""):
    print(text, flush=True)


def online_env():
    """The environment without Offline mode, for the programs that download updates."""
    env = dict(os.environ)
    for key in cfg.OFFLINE_ENV:
        env.pop(key, None)
    return env


def run(command, timeout=600, show=False):
    """Run a program; return (exit code, output). With show=True its output is printed as it comes."""
    try:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   encoding="utf-8", errors="replace", env=online_env(),
                                   stdin=subprocess.DEVNULL, creationflags=cfg.NO_WINDOW)
    except OSError as error:
        return 1, str(error)
    lines = []
    for line in process.stdout:
        lines.append(line)
        if show and line.strip() and not re.search(r"[█▒━]|\d+%\s*$", line):
            print("    " + line.rstrip()[:200], flush=True)
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return 1, "".join(lines) + "\n(stopped: it took too long)"
    return code, "".join(lines)


def http_get(url, headers=None, timeout=30):
    request = urllib.request.Request(url, headers={"User-Agent": "local-ai-updates", **(headers or {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def version_key(text):
    return [int(n) for n in re.findall(r"\d+", str(text))[:4]]


def item(key, name, kind, current, latest, update, note="", default=True):
    return {"key": key, "name": name, "kind": kind, "current": current, "latest": latest,
            "update": update, "note": note, "default": default}


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_winget():
    if not shutil.which("winget"):
        return [], ["winget isn't available, so Ollama, Git, Tesseract, Poppler and uv weren't checked."]
    code, out = run(["winget", "upgrade", "--include-unknown", "--accept-source-agreements",
                     "--disable-interactivity"], timeout=180)
    found = []
    for line in out.splitlines():
        tokens = line.split()
        for package, name in WINGET_PROGRAMS.items():
            if package in tokens:
                i = tokens.index(package)
                if len(tokens) > i + 2:
                    found.append(item(package.lower(), name, "program", tokens[i + 1], tokens[i + 2],
                                      ["winget", package]))
    return found, []


def check_hermes():
    hermes = cfg.find_hermes()
    if not hermes:
        return [], ["Hermes isn't installed."]
    _, version = run([hermes, "--version"], timeout=60)
    current = (re.search(r"v?\d+\.\d+\.\d+", version) or [""])[0] or version.strip()[:40]
    code, out = run([hermes, "update", "--check"], timeout=240)
    found, notes = [], []
    if re.search(r"available", out, re.I) and not re.search(r"up to date|no newer", out, re.I):
        latest = (re.search(r"available:\s*(.+)", out) or [None, "newer version"])[1].strip()
        found.append(item("hermes", "Hermes (and its memory plugin)", "program", current, latest, ["hermes"],
                          "Close Hermes first. Offline mode pauses until the memory test passes again."))
    elif code != 0:
        notes.append("Hermes couldn't check for updates: " + out.strip().splitlines()[-1][:200]
                     if out.strip() else "Hermes couldn't check for updates.")
    code, out = run([hermes, "plugins", "check-updates"], timeout=240)
    for line in out.splitlines():
        if "hindsight" in line.lower() and re.search(r"update|newer|available", line, re.I) \
                and not re.search(r"up to date|no update", line, re.I):
            found.append(item("hindsight-plugin", "Hindsight memory plugin", "program", "installed",
                              line.strip()[:80], ["hermes-plugin"],
                              "Offline mode pauses until the memory test passes again."))
            break
    return found, notes


def goose_version():
    exe = cfg.find_goose()
    if not exe:
        return None
    if os.name == "nt":
        code, out = run(["powershell", "-NoProfile", "-Command",
                         f"(Get-Item '{exe}').VersionInfo.ProductVersion"], timeout=60)
        if code == 0 and out.strip():
            return out.strip().splitlines()[-1]
    return "installed"


def goose_release():
    for repo in GOOSE_REPOS:
        try:
            data = json.loads(http_get(f"https://api.github.com/repos/{repo}/releases/latest"))
            zips = [a for a in data.get("assets", []) if re.search(r"win32.*\.zip$", a["name"], re.I)]
            zips.sort(key=lambda a: "cuda" in a["name"].lower())   # the plain build first
            if zips:
                return data.get("tag_name", ""), zips[0]["browser_download_url"]
        except Exception:
            continue
    return None, None


def check_goose():
    current = goose_version()
    if current is None:
        return [], []
    latest, _ = goose_release()
    if not latest:
        return [], ["Couldn't reach GitHub to check Goose."]
    if current == "installed" or version_key(latest) > version_key(current):
        return [item("goose", "Goose", "program", current, latest, ["goose"], "Close Goose first.")], []
    return [], []


def check_python():
    code, out = run([python_console(), "-m", "pip", "list", "--outdated", "--format", "json"], timeout=180)
    try:
        outdated = {p["name"].lower(): p for p in json.loads(out[out.find("["):])}
    except ValueError:
        return [], ["Couldn't check the Python packages."]
    found = []
    for name in PYTHON_PACKAGES:
        if name in outdated:
            p = outdated[name]
            found.append(item("py-" + name, f"Python package {name}", "program", p.get("version"),
                              p.get("latest_version"), ["pip", name]))
    return found, []


def registry_path(model):
    name, _, tag = model.partition(":")
    if "/" not in name:
        name = "library/" + name
    return f"{name.lower()}/manifests/{tag or 'latest'}"


def check_models():
    try:
        tags = cfg.http_json(cfg.OLLAMA + "/api/tags", timeout=10)["models"]
    except Exception:
        return [], ["Ollama isn't running, so the models weren't checked."]
    local = {m["name"].removesuffix(":latest"): m.get("digest", "") for m in tags}
    found, notes = [], []
    for model in {**cfg.REQUIRED_MODELS, **cfg.EXTRA_MODELS}:
        if model not in local:
            continue
        try:
            manifest = http_get(f"{REGISTRY}/{registry_path(model)}", {"Accept": MANIFEST_TYPE})
        except Exception:
            notes.append(f"Couldn't check {model} on ollama.com.")
            continue
        remote = hashlib.sha256(manifest).hexdigest()
        if remote != local[model].removeprefix("sha256:"):
            size = sum(layer.get("size", 0) for layer in json.loads(manifest).get("layers", []))
            found.append(item("model:" + model, model, "model", None, "a newer version is on ollama.com",
                              ["model", model], f"Download about {size / 1e9:.0f} GB, then the copies "
                              "are remade.", default=False))
    return found, notes


def check_all():
    """Returns (items, notes). Each item can be passed to update()."""
    items, notes = [], []
    for check in (check_winget, check_hermes, check_goose, check_python, check_models):
        try:
            found, more = check()
        except Exception as error:   # one failing check shouldn't stop the others
            found, more = [], [f"{check.__name__}: {error}"]
        items += found
        notes += more
    return items, notes


# --------------------------------------------------------------------------
# Updates
# --------------------------------------------------------------------------
def python_console():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def pause_offline_mode():
    """After a Hermes or plugin update the memory server may need to download its new version."""
    if cfg.READY_FILE.exists():
        cfg.READY_FILE.unlink()
    cfg.set_offline_settings(cfg.load_choices())
    say("  Offline mode is paused. Run \"Test memory\" once with the internet on; it turns back on by itself.")


def update_goose():
    latest, url = goose_release()
    if not url:
        say("  [X] Couldn't find the Goose download on GitHub.")
        return False
    say(f"  Downloading Goose {latest}...")
    with tempfile.TemporaryDirectory() as folder:
        archive = Path(folder) / "goose.zip"
        archive.write_bytes(http_get(url, timeout=600))
        target = cfg.GOOSE_APP_DIR
        target.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive) as z:
                z.extractall(target)
        except PermissionError:
            say("  [X] Goose is still open. Close it (also in the tray) and update again.")
            return False
    say(f"  [OK] Goose {latest} installed in {target}")
    return True


def update(chosen):
    """Install the chosen items (dicts from check_all). Returns True when all worked."""
    all_ok, models = True, []
    for entry in chosen:
        kind, *args = entry["update"]
        say(f"Updating {entry['name']}...")
        if kind == "winget":
            code, _ = run(["winget", "upgrade", "--id", args[0], "-e", "--silent", "--accept-source-agreements",
                           "--accept-package-agreements", "--disable-interactivity"], timeout=1800, show=True)
            ok = code == 0
            if ok and args[0] == "Ollama.Ollama":
                cfg.restart_ollama()       # starts it again with your settings
        elif kind == "hermes":
            code, _ = run([cfg.find_hermes(), "update", "--yes"], timeout=3600, show=True)
            ok = code == 0
            run([cfg.find_hermes(), "plugins", "update", "hindsight"], timeout=600, show=True)
            pause_offline_mode()
        elif kind == "hermes-plugin":
            code, _ = run([cfg.find_hermes(), "plugins", "update", "hindsight"], timeout=600, show=True)
            ok = code == 0
            pause_offline_mode()
        elif kind == "goose":
            ok = update_goose()
        elif kind == "pip":
            code, _ = run([python_console(), "-m", "pip", "install", "--user", "--upgrade", args[0]],
                          timeout=900, show=True)
            ok = code == 0
        elif kind == "model":
            ollama = cfg.find_ollama()
            say("  Downloading (this can take a long time)...")
            code, out = run([ollama, "pull", args[0]], timeout=6 * 3600)
            ok = code == 0
            if ok:
                models.append(args[0])
            else:
                say("    " + out.strip().splitlines()[-1][:200] if out.strip() else "")
        else:
            ok = False
        say(f"  [{'OK' if ok else 'X'}] {entry['name']}")
        all_ok = all_ok and ok
    if models:   # the copies still point at the old files; make them again from the new download
        c = cfg.load_choices()
        installed = cfg.installed_models() or set()
        for name, (source, ctx) in cfg.wanted_copies(c, installed).items():
            if source in models:
                cfg.make_copy(name, ctx)
    say("Putting your settings back (an update can change them)...")
    cfg.apply_choices(cfg.load_choices(), restart=False)
    return all_ok


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "update"):
        print(__doc__)
        return 1
    items, notes = check_all()
    if sys.argv[1] == "check":
        for entry in items:
            say(f"  {entry['key']:<28} {entry['name']}: " + (f"now {entry['current']}, new {entry['latest']}"
                                                            if entry["current"] else entry["latest"]))
        for note in notes:
            say(f"  [!] {note}")
        say("Everything is up to date." if not items else f"{len(items)} update(s) available.")
        return 0
    wanted = {k.lower() for k in sys.argv[2:]}
    chosen = [e for e in items if e["key"] in wanted or e["key"].split(".")[0] in wanted]
    if not chosen:
        say("Nothing to update (or those names aren't out of date).")
        return 0
    return 0 if update(chosen) else 1


if __name__ == "__main__":
    sys.exit(main())
