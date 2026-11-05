#!/usr/bin/env python3
"""
memory_quick_test.py - a quick test of Hindsight long-term memory (usually 3-10 minutes,
instead of an hour for all tests).

It talks to Hindsight's memory server directly:
  1. makes sure the server has the right settings (15-minute timeout, one request at a time,
     its search models on the CPU) and that it can start;
  2. clears the saves left over from earlier memory TESTS (only the test memory, never yours);
  3. saves one test fact and measures how long saving takes;
  4. asks for it back, then saves a correction and asks again.

Your real memories are not touched: it uses a separate test memory ("bank") that it removes
afterwards. Nothing needs your password.

HOW TO RUN, in the folder with the Local AI scripts (local-ai-windows in %LOCALAPPDATA%):
    py memory_quick_test.py

The result is also saved in memory_quick_test_log.txt next to this script.
"""

import json
import random
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import windows_configure as setup  # noqa: E402  (paths and settings of the Windows setup)

LOG = HERE / "memory_quick_test_log.txt"
TEST_BANKS = ("setup-test",)          # test memories made by earlier test runs
LINES = []


def say(text=""):
    print(text, flush=True)
    LINES.append(text)


def server_port():
    try:
        for line in setup.HINDSIGHT_PROFILE_ENV.read_text().splitlines():
            if line.startswith("HINDSIGHT_API_PORT="):
                return int(line.split("=", 1)[1].strip())
    except (OSError, ValueError):
        pass
    return 9177


def port_open(port):
    with socket.socket() as sock:
        sock.settimeout(2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def call(base, method, path, body=None, timeout=60):
    request = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")[:300]


def find_paths(base):
    """Find the retain, recall, bank and operations paths in the server's own API description."""
    status, spec = call(base, "GET", "/openapi.json")
    if status != 200 or not isinstance(spec, dict):
        return {}
    try:   # kept so the exact API can be looked up if something doesn't match
        (HERE / "hindsight_openapi.json").write_text(json.dumps(spec, indent=1))
    except OSError:
        pass
    found = {}
    for path, methods in spec.get("paths", {}).items():
        if "{bank_id}" not in path:
            continue
        if path.endswith("/memories/recall") and "post" in methods:
            found["recall"] = path
        elif path.endswith("/memories") and "post" in methods:
            found["retain"] = path
        elif path.endswith("/banks/{bank_id}") and "delete" in methods:
            found["delete_bank"] = path
        elif path.endswith("/operations") and "get" in methods:
            found["list_ops"] = path
        elif "{operation_id}" in path and "delete" in methods:
            found["cancel_op"] = path
    return found


def with_bank(path, bank):
    return path.replace("{bank_id}", urllib.parse.quote(bank))


def start_server(hermes, port, bank):
    """The server starts with a Hermes chat. Point that chat at the test memory first."""
    say("  Starting Hindsight's memory server (with one short Hermes chat)...")
    original = setup.HINDSIGHT_CONFIG.read_text()
    try:
        data = json.loads(original)
        data["bank_id"] = bank
        setup.HINDSIGHT_CONFIG.write_text(json.dumps(data, indent=2) + "\n")
        subprocess.run([hermes, "chat", "-q", "Reply with the word OK."], capture_output=True,
                       text=True, timeout=600, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        pass
    finally:
        setup.HINDSIGHT_CONFIG.write_text(original)
    for _ in range(90):
        if port_open(port):
            return True
        time.sleep(2)
    return False


def main():
    started = time.time()
    say("Hindsight memory: quick test")
    say("=" * 40)
    settings = setup.load_settings()
    model, endpoint = setup.memory_target(settings)
    hermes = setup.find_hermes()
    if not hermes:
        say("  [X] Hermes isn't installed.")
        return 1

    say("1. Settings")
    setup.ensure_uvx()
    setup.configure_hindsight_server(model, endpoint)
    say(f"  Memory model: {model} at {endpoint}")

    say("")
    say("2. The memory server")
    port = server_port()
    bank = f"quick-test-{random.randint(100000, 999999)}"
    if not port_open(port) and not start_server(hermes, port, bank):
        say(f"  [X] The memory server didn't start. The reason is in {setup.HINDSIGHT_EMBED_LOG}")
        return 1
    base = f"http://127.0.0.1:{port}"
    paths = find_paths(base)
    say(f"  [OK] Running at {base}")
    if "retain" not in paths or "recall" not in paths:
        say("  [X] Couldn't find the save and search functions in the server's API: "
            + json.dumps(paths))
        return 1

    say("")
    say("3. Clearing saves left over from earlier tests (test memories only)")
    for old in TEST_BANKS:
        if "delete_bank" in paths:
            status, _ = call(base, "DELETE", with_bank(paths["delete_bank"], old), timeout=120)
            say(f"  Test memory '{old}': " + ("removed" if status in (200, 202, 204) else
                                              "nothing to remove" if status == 404 else f"answer {status}"))
    if "list_ops" in paths:
        status, ops = call(base, "GET", with_bank(paths["list_ops"], "hermes"))
        items = ops.get("operations", ops.get("items", [])) if isinstance(ops, dict) else ops
        waiting = [o for o in (items or []) if isinstance(o, dict)
                   and str(o.get("status", "")).lower() in ("pending", "processing", "failed", "retrying")]
        say(f"  Saves still waiting in your real memory: {len(waiting)} (kept; they continue in the background)")

    word1 = f"{random.choice(['harbor', 'lantern', 'copper', 'falcon', 'willow'])}-{random.randint(1000, 9999)}"
    word2 = f"{random.choice(['meadow', 'granite', 'orchid', 'cedar', 'comet'])}-{random.randint(1000, 9999)}"

    def save(text):
        t0 = time.time()
        status, answer = call(base, "POST", with_bank(paths["retain"], bank),
                              {"items": [{"content": text}], "async": False}, timeout=1200)
        return status, answer, time.time() - t0

    def recall(word):
        status, answer = call(base, "POST", with_bank(paths["recall"], bank),
                              {"query": "What is the user's test code word?"}, timeout=300)
        return word in json.dumps(answer).lower(), status

    ok = True
    try:
        say("")
        say("4. Saving a test fact (the memory model reads it; this is the slow part)")
        status, answer, seconds = save(f"The user said: my test code word is {word1}.")
        if status not in (200, 201, 202):
            say(f"  [X] Saving failed after {seconds:.0f} seconds (answer {status}): {str(answer)[:200]}")
            ok = False
        else:
            say(f"  [OK] Saved in {seconds:.0f} seconds")
            found, _ = recall(word1)
            say(f"  [{'OK' if found else 'X'}] Found it again: {'yes' if found else 'no'}")
            ok = ok and found
            say("")
            say("5. Saving a correction")
            status, answer, seconds = save(f"Correction from the user: my test code word is now {word2}, "
                                           f"not {word1}.")
            if status not in (200, 201, 202):
                say(f"  [X] Saving failed after {seconds:.0f} seconds (answer {status}): {str(answer)[:200]}")
                ok = False
            else:
                say(f"  [OK] Saved in {seconds:.0f} seconds")
                found, _ = recall(word2)
                say(f"  [{'OK' if found else 'X'}] Finds the corrected word: {'yes' if found else 'no'}")
                ok = ok and found
    finally:
        if "delete_bank" in paths:
            call(base, "DELETE", with_bank(paths["delete_bank"], bank), timeout=120)

    say("")
    say("=" * 40)
    minutes = (time.time() - started) / 60
    if ok:
        say(f"RESULT: memory works. Saving and finding facts both worked ({minutes:.0f} minutes).")
        if hasattr(setup, "after_memory_test_passed"):   # Windows: turns on Offline mode if chosen
            try:
                setup.after_memory_test_passed()
            except Exception as error:  # noqa: BLE001
                say(f"  [!] Couldn't turn on Offline mode: {error}")
    else:
        say(f"RESULT: memory does NOT work yet ({minutes:.0f} minutes). The details are in")
        say(f"{LOG.name} (next to this script) and {setup.HINDSIGHT_SERVER_LOG}")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        say("Stopped.")
        code = 1
    finally:
        try:
            LOG.write_text("\n".join(LINES) + "\n")
        except OSError:
            pass
    sys.exit(code)
