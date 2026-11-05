#!/usr/bin/env python3
"""
local_ai_settings.pyw - a settings window for Local AI on Windows (Ollama + Hermes + Hindsight
memory + the document tool).

Double-click this file, or open "Local AI Settings" from the Start menu. Pick a setting from
each dropdown, read what it does underneath, then click "Save and apply".

Every setting is marked:
    Safe to change        try it; "Recommended settings" puts everything back
    Usually leave as is   only change it for a reason

Your choices are kept in local_ai_choices.json in Hermes's folder, so running the setup again
keeps them. Nothing here deletes models. Memories are deleted only with "Clear memories..." on
the Memory tab, which asks first and keeps a backup unless you untick it.
"""

import io
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

if sys.stdout is None:          # pythonw.exe has no console
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import tkinter as tk                         # noqa: E402
from tkinter import messagebox, ttk          # noqa: E402
from tkinter.scrolledtext import ScrolledText  # noqa: E402

import windows_configure as cfg  # noqa: E402
import local_ai_updates as updates  # noqa: E402

WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------
# What each model is good at (shown under the dropdown when you pick it)
# --------------------------------------------------------------------------
MODEL_NOTES = {
    "same": "Uses the chat model, so the graphics card never has to swap models. Fastest.",
    "gemma4:26b-128k": "Gemma 4 26B. The best balance of quality and speed on a 12 GB card. Sees photos.",
    "qwen3.8:64k": "Qwen3.8 27B. Newest and most careful, but bigger than 12 GB, so part of it runs "
                      "on the processor: several times slower. Sees photos.",
    "gemma4:e4b-128k": "Gemma 4 E4B. Small and quick, but less careful and less reliable at "
                         "saving memories.",
    "qwen3-vl:8b-32k": "Qwen3-VL 8B. Quick at looking at photos and spotting tables, weaker at using "
                       "tools in a chat.",
    "nemotron-3.5-lightning:64k": "Nemotron 3.5 Lightning. Fast and good with text and numbers. Can't see photos.",
    "glm-ocr:16k": "GLM-OCR. Reads printed and handwritten text very accurately. Can't chat.",
}
BIG = ["qwen3.8:64k"]
SEEING = ["gemma4:26b-128k"] + BIG + ["gemma4:e4b-128k", "qwen3-vl:8b-32k"]

SAFE, LEAVE = "Safe to change", "Usually leave as is"

# key, tab, title, hint, what the setting does, options as (value, label, note)
def model_options(names):
    return [(n, n, MODEL_NOTES.get(n, "")) for n in names]


SETTINGS = [
    ("chat_model", "Chat", "Chat model", SAFE,
     "The model you talk to in Hermes. Unless you pick something else below, it also looks at your "
     "photos and saves your long-term memories.",
     model_options(["gemma4:26b-128k"] + BIG + ["gemma4:e4b-128k", "qwen3-vl:8b-32k"])),
    ("chat_context", "Chat", "Conversation length", LEAVE,
     "How much of the chat the model keeps in mind at once (1K is about 750 words). Longer remembers "
     "more of a long chat, but uses more graphics card memory and makes replies slower.",
     [("default", "Model default", "gemma4:26b-128k and gemma4:e4b-128k: 128K. qwen3.8:64k: 64K."),
      ("32768", "32K", "Short chats only. Hermes may forget the start of a long chat."),
      ("65536", "64K", "Enough for most chats with a few photos."),
      ("131072", "128K", "Long chats and many photos. The most a 12 GB card handles well."),
      ("262144", "256K", "Very long chats. Uses much more memory, and replies get a lot slower.")]),
    ("photo_model", "Chat", "Looks at photos in a chat", SAFE,
     "Which model answers general questions about a photo, such as \"What is this?\". For exact text "
     "and numbers Hermes uses the document tool instead (see the Documents tab).",
     [("same", "Same as the chat model", MODEL_NOTES["same"])] + model_options(SEEING)),
    ("helper_model", "Chat", "Helper for side tasks", LEAVE,
     "Hermes hands smaller jobs to this model, such as summarizing a long file, while the chat model "
     "stays in charge.",
     model_options(["nemotron-3.5-lightning:64k", "gemma4:26b-128k"] + BIG + ["gemma4:e4b-128k"])),

    ("memory_model", "Memory", "Saves long-term memories", LEAVE,
     "After each reply, this model reads the chat and saves the facts worth keeping. Rules don't "
     "depend on it: they are in SOUL.md, which Hermes reads at the start of every chat. A model "
     "different from the chat model makes replies wait while the graphics card swaps models.",
     [("same", "Same as the chat model", MODEL_NOTES["same"])]
     + model_options(["gemma4:26b-128k"] + BIG + ["gemma4:e4b-128k", "nemotron-3.5-lightning:64k"])),

    ("docs_profile", "Documents", "Quality", SAFE,
     "How carefully the document tool reads photos when you ask for exact text or numbers.",
     [("quality", "Quality", "Every page is read twice and every value is checked. About twice as slow; "
                             "use it for hard-to-read photos."),
      ("balanced", "Balanced", "The second reading is only done for unclear pages, and every value is "
                               "still checked. In the test: about 1.5 minutes per photo, 12 of 12 values found.")]),
    ("docs_ocr", "Documents", "Reads the text", LEAVE,
     "The model that reads the words and numbers in each photo. This is the main reading.",
     model_options(["glm-ocr:16k", "qwen3-vl:8b-32k"])),
    ("docs_vision", "Documents", "Second reading", SAFE,
     "Looks at each photo, reads it a second time and spots tables. The two readings are compared.",
     model_options(["qwen3-vl:8b-32k", "gemma4:26b-128k"] + BIG)),
    ("docs_processor", "Documents", "Finds the values", LEAVE,
     "Turns the text into values (total, date, line items). Each value must come with the exact words "
     "it came from, so the check can confirm it.",
     model_options(["nemotron-3.5-lightning:64k", "gemma4:26b-128k"] + BIG)),
    ("docs_reviewer", "Documents", "Reviewer", LEAVE,
     "Looks at the photo again for any value the check found doubtful.",
     model_options(["gemma4:26b-128k"] + BIG + ["qwen3-vl:8b-32k"])),

    ("keep_alive", "Graphics card", "Unload an unused model after", SAFE,
     "A model you keep using stays on the graphics card. This is how long it stays there after your "
     "last message. Loading it again takes a few seconds to a minute.",
     [("5m", "5 minutes", "Frees the graphics card soon, for example for games."),
      ("30m", "30 minutes", "Stays loaded during normal breaks."),
      ("1h", "1 hour", "Stays loaded during longer breaks."),
      ("4h", "4 hours", "Almost always loaded while the PC is on."),
      ("-1", "Never", "Stays loaded until Ollama stops or another model is needed.")]),
    ("gpu_free_mb", "Graphics card", "Graphics card memory kept free", LEAVE,
     "Memory Ollama leaves free for Windows, the desktop and large photos. Less free memory fits more "
     "of a model, but can crash a reply with an out-of-memory error.",
     [("512", "512 MB", "More room for the model. Can run out of memory with large photos."),
      ("1024", "1 GB", "Tested on a 12 GB card: no out-of-memory errors."),
      ("2048", "2 GB", "Use this if you see out-of-memory errors.")]),

    ("offline", "Offline", "Offline mode", SAFE,
     "Ollama, the models, Hermes, your memories and the document tool always run on this PC. The memory "
     "server downloads its program and two search models the first time it starts; Offline mode then "
     "makes it use only those saved copies, and Hermes stops checking for updates. It turns on by "
     "itself when the memory test passes.",
     [("on", "On", "Nothing is downloaded. Web searches in Hermes still need the internet. To update "
                    "Hermes, turn this off first (the setup turns it off by itself while it runs)."),
      ("off", "Off", "The memory server and Hermes may check online for updates.")]),

    ("goose_model", "Goose", "Goose chat model", SAFE,
     "Goose is a second AI app with its own rules and memory. It reads photos and documents with the same "
     "document tool. This is the model it talks with.",
     [("same", "Same as the Hermes chat model", "Uses the model already loaded for Hermes, so switching "
                                                 "between the apps doesn't swap models.")]
     + model_options(["gemma4:26b-128k"] + BIG + ["gemma4:e4b-128k"])),
    ("goose_mode", "Goose", "Asks before acting", SAFE,
     "Goose can run commands and change files on this PC. This sets when it asks you first.",
     [("smart_approve", "When it matters", "Asks before commands or changes that look risky; reading "
                                           "files doesn't need a click."),
      ("approve", "Every time", "Asks before every command and every file change. Safest, more clicks."),
      ("auto", "Never", "Acts without asking. Only for tasks you trust completely."),
      ("chat", "Chat only", "No commands or file changes, so the document tool can't run.")]),
]
TABS = ["Chat", "Memory", "Documents", "Graphics card", "Offline", "Goose", "Updates"]

TOOLTIP_COLORS = {SAFE: "#1b7f3b", LEAVE: "#777777"}


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
class QueueWriter(io.TextIOBase):
    """Sends printed text to the window's output box."""

    def __init__(self, target):
        self.target = target

    def write(self, text):
        if text:
            self.target.put(("text", text))
        return len(text)


def python_console():
    """python.exe for running the tests (pythonw.exe can't show their output)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def script(name):
    for folder in (HERE, cfg.APP_DIR):
        if (folder / name).exists():
            return folder / name
    return HERE / name


def loaded_models():
    try:
        with urllib.request.urlopen(cfg.OLLAMA + "/api/ps", timeout=3) as response:
            return [(m["name"].split(":latest")[0], m.get("size_vram", 0))
                    for m in json.loads(response.read()).get("models", [])]
    except Exception:
        return None


def open_file(path):
    path = Path(path)
    if WINDOWS:
        if path.is_dir():
            os.startfile(str(path))
        else:
            subprocess.Popen(["notepad.exe", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


# --------------------------------------------------------------------------
# The window
# --------------------------------------------------------------------------
class SettingsApp:
    def __init__(self, root):
        self.root = root
        self.messages = queue.Queue()
        self.busy = False
        self.installed = cfg.installed_models()
        self.saved = cfg.load_choices()
        self.vars, self.boxes, self.notes, self.labels, self.options = {}, {}, {}, {}, {}

        root.title("Local AI settings")
        root.minsize(760, 640)
        style = ttk.Style(root)
        if WINDOWS:
            style.theme_use("vista")
        base_font = ("Segoe UI", 10) if WINDOWS else ("TkDefaultFont", 10)
        self.bold = (base_font[0], 10, "bold")
        self.small = (base_font[0], 9)
        root.option_add("*Font", base_font)

        outer = ttk.Frame(root, padding=12)
        outer.pack(fill="both", expand=True)

        intro = ("Pick a setting from each dropdown and read what it does underneath. Nothing changes "
                 "until you click \"Save and apply\".")
        ttk.Label(outer, text=intro, wraplength=720).pack(anchor="w")
        legend = ttk.Frame(outer)
        legend.pack(anchor="w", pady=(4, 8))
        tk.Label(legend, text=SAFE, fg=TOOLTIP_COLORS[SAFE], font=self.bold).pack(side="left")
        ttk.Label(legend, text=": try it freely.   ").pack(side="left")
        tk.Label(legend, text=LEAVE, fg=TOOLTIP_COLORS[LEAVE], font=self.bold).pack(side="left")
        ttk.Label(legend, text=": only change it for a reason.").pack(side="left")

        status = ttk.Frame(outer)
        status.pack(fill="x", pady=(0, 6))
        self.status = ttk.Label(status, text="", font=self.small)
        self.status.pack(side="left")
        ttk.Button(status, text="Refresh", command=self.refresh_status).pack(side="right")

        tabs = ttk.Notebook(outer)
        tabs.pack(fill="x")
        frames = {}
        for name in TABS:
            frame = ttk.Frame(tabs, padding=10)
            frame.columnconfigure(1, weight=1)
            tabs.add(frame, text=name)
            frames[name] = frame
        rows = {name: 0 for name in TABS}
        for key, tab, title, hint, about, options in SETTINGS:
            self.add_setting(frames[tab], rows[tab], key, title, hint, about, options)
            rows[tab] += 3
        tools = ttk.Frame(frames["Memory"])
        tools.grid(row=rows["Memory"], column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(frames["Memory"], text="Rules you gave Hermes (\"Make this a rule\") are in SOUL.md, "
                  "under \"My rules\". Why each rule changed is under \"Rule changes\".",
                  wraplength=700, font=self.small).grid(row=rows["Memory"] + 1, column=0, columnspan=3,
                                                         sticky="w", pady=(6, 0))
        ttk.Button(tools, text="Edit rules (SOUL.md)",
                   command=lambda: open_file(cfg.SOUL_FILE)).pack(side="left")
        ttk.Button(tools, text="Test memory (3-10 min)", command=self.test_memory).pack(side="left", padx=6)
        ttk.Button(tools, text="Clear memories...", command=self.clear_memory_dialog).pack(side="left")
        ready, why = cfg.offline_ready()
        active = all(os.environ.get(k) == v for k, v in cfg.OFFLINE_ENV.items())
        state = ("Offline mode is on now." if active else
                 "Ready: click Save and apply to turn Offline mode on." if ready else
                 f"Offline mode isn't on yet: it {why}. Run \"Test memory\" on the Memory tab "
                 "with the internet connected.")
        ttk.Label(frames["Offline"], text=state, wraplength=700, font=self.bold).grid(
            row=rows["Offline"], column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Label(frames["Offline"], text="The memory server sends nothing about you: it has no usage statistics, "
                  "its trace sending is turned off, and it talks only to Ollama on this PC. Offline mode also stops "
                  "it from downloading anything.", wraplength=700, font=self.small).grid(
            row=rows["Offline"] + 1, column=0, columnspan=3, sticky="w", pady=(10, 0))
        goose_tools = ttk.Frame(frames["Goose"])
        goose_tools.grid(row=rows["Goose"], column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(goose_tools, text="Edit Goose's rules (.goosehints)",
                   command=lambda: open_file(cfg.GOOSE_HINTS)).pack(side="left")
        ttk.Label(frames["Goose"], text=("Goose's rules are in .goosehints, under \"My rules\", and why each "
                  "changed is under \"Rule changes\". Its memories are saved by its Memory extension. Both "
                  "are separate from Hermes." if cfg.find_goose() else
                  "Goose isn't installed. Run the setup again to install it."),
                  wraplength=700, font=self.small).grid(row=rows["Goose"] + 1, column=0, columnspan=3,
                                                         sticky="w", pady=(6, 0))
        self.build_updates_tab(frames["Updates"])
        gpu_tools = ttk.Frame(frames["Graphics card"])
        gpu_tools.grid(row=rows["Graphics card"], column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(gpu_tools, text="Free the graphics card now",
                   command=lambda: self.run_in_background("Freeing the graphics card", cfg.free_graphics_card)
                   ).pack(side="left")
        ttk.Label(frames["Graphics card"], text="Unloads every model now (the next message loads one again), and "
                  "stops Ollama's model runners (llama-server.exe) that were left behind.", wraplength=700,
                  font=self.small).grid(row=rows["Graphics card"] + 1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        docs_tools = ttk.Frame(frames["Documents"])
        docs_tools.grid(row=rows["Documents"], column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(docs_tools, text="Test the document tool (about 5 min)",
                   command=self.test_docs).pack(side="left")

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=10)
        self.save_button = ttk.Button(buttons, text="Save and apply", command=self.save)
        self.save_button.pack(side="left")
        ttk.Button(buttons, text="Recommended settings", command=self.recommended).pack(side="left", padx=6)
        ttk.Button(buttons, text="Undo changes", command=self.undo).pack(side="left")
        ttk.Button(buttons, text="Check everything", command=self.check).pack(side="left", padx=6)
        ttk.Button(buttons, text="Open Hermes's folder",
                   command=lambda: open_file(cfg.HERMES_DIR)).pack(side="right")

        self.output = ScrolledText(outer, height=12, wrap="word", font=("Consolas", 9) if WINDOWS else None)
        self.output.pack(fill="both", expand=True)
        self.output.configure(state="disabled")

        self.show_choices(self.saved)
        self.refresh_status()
        self.root.after(100, self.poll)

    # ---- building the rows ------------------------------------------------
    def label_for(self, key, value, label):
        tags = []
        if str(cfg.DEFAULT_CHOICES[key]) == value:
            tags.append("recommended")
        if self.installed is not None and value in cfg.ALL_COPIES:
            source = cfg.ALL_COPIES[value][0]
            if value not in self.installed and source not in self.installed:
                tags.append("not downloaded")
        return label + (f"   ({', '.join(tags)})" if tags else "")

    def add_setting(self, frame, row, key, title, hint, about, options):
        ttk.Label(frame, text=title, font=self.bold).grid(row=row, column=0, sticky="w", pady=(8, 0))
        labels = [self.label_for(key, value, label) for value, label, _ in options]
        var = tk.StringVar()
        box = ttk.Combobox(frame, textvariable=var, values=labels, state="readonly", width=48)
        box.grid(row=row, column=1, sticky="w", padx=10, pady=(8, 0))
        tk.Label(frame, text=hint, fg=TOOLTIP_COLORS[hint], font=self.small).grid(
            row=row, column=2, sticky="e", pady=(8, 0))
        ttk.Label(frame, text=about, wraplength=700, font=self.small).grid(
            row=row + 1, column=0, columnspan=3, sticky="w")
        note = tk.Label(frame, text="", fg="#1f5fa8", font=self.small, wraplength=700, justify="left")
        note.grid(row=row + 2, column=0, columnspan=3, sticky="w")
        self.vars[key], self.boxes[key], self.notes[key], self.options[key] = var, box, note, options
        self.labels[key] = list(zip([o[0] for o in options], labels, [o[2] for o in options]))
        box.bind("<<ComboboxSelected>>", lambda event, k=key: self.update_note(k))

    def refresh_labels(self):
        """Update the "(not downloaded)" marks, keeping what each dropdown shows."""
        for key, options in self.options.items():
            chosen = self.value_of(key)
            labels = [self.label_for(key, value, label) for value, label, _ in options]
            self.labels[key] = list(zip([o[0] for o in options], labels, [o[2] for o in options]))
            self.boxes[key].configure(values=labels)
            self.show_one(key, chosen)

    def value_of(self, key):
        shown = self.vars[key].get()
        for value, label, _ in self.labels[key]:
            if label == shown:
                return value
        return str(cfg.DEFAULT_CHOICES[key])

    def update_note(self, key):
        shown = self.vars[key].get()
        for value, label, note in self.labels[key]:
            if label == shown:
                self.notes[key].configure(text="This choice: " + note if note else "")
        if key == "chat_model":
            self.update_note("chat_context")

    def show_one(self, key, want):
        labels = self.labels[key]
        match = next((label for value, label, _ in labels if value == want), None)
        if match is None:     # a value set by hand that isn't in the list: show it too
            labels.append((want, want, "Set by hand."))
            self.boxes[key].configure(values=[label for _, label, _ in labels])
            match = want
        self.vars[key].set(match)
        self.update_note(key)

    def show_choices(self, choices):
        for key in self.vars:
            self.show_one(key, str(choices.get(key, cfg.DEFAULT_CHOICES[key])))

    def current(self):
        choices = {key: self.value_of(key) for key in self.vars}
        choices["gpu_free_mb"] = int(choices["gpu_free_mb"])
        return choices

    # ---- status and output ------------------------------------------------
    def refresh_status(self):
        self.installed = cfg.installed_models()
        if self.installed is None:
            self.status.configure(text="Ollama isn't answering. Start \"Ollama\" from the Start menu, then "
                                       "click Refresh.")
            return
        if self.options:
            self.refresh_labels()
        loaded = loaded_models() or []
        now = ", ".join(f"{name} ({size / 1024 ** 3:.1f} GB on the graphics card)" for name, size in loaded)
        self.status.configure(text="Ollama is running. Loaded now: " + (now or "nothing"))

    def write(self, text):
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def poll(self):
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "text":
                    self.write(payload)
                elif kind == "call":
                    payload()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def run_in_background(self, title, work, done=None):
        """Run work() without freezing the window; its printed output goes to the output box."""
        if self.busy:
            messagebox.showinfo("Please wait", "Something is still running. Wait until it finishes.")
            return
        self.busy = True
        self.save_button.configure(state="disabled")
        self.write(f"\n=== {title} ===\n")
        writer = QueueWriter(self.messages)

        def worker():
            result = None
            try:
                with redirect_stdout(writer), redirect_stderr(writer):
                    result = work()
            except BaseException as error:  # noqa: BLE001 (also SystemExit from a missing module)
                writer.write(f"\n[X] {error}\n{traceback.format_exc()}\n")
            self.messages.put(("call", lambda: self.finished(done, result)))

        threading.Thread(target=worker, daemon=True).start()

    def finished(self, done, result):
        self.busy = False
        self.save_button.configure(state="normal")
        self.refresh_status()
        if done:
            done(result)

    def run_program(self, title, command):
        def work():
            cfg.start_ollama()   # the tests need Ollama; it may not have started with Windows
            env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                       encoding="utf-8", errors="replace", env=env,
                                       stdin=subprocess.DEVNULL, creationflags=cfg.NO_WINDOW)
            for line in process.stdout:
                print(line, end="")
            return process.wait()
        self.run_in_background(title, work)

    def clear_memory_dialog(self):
        window = tk.Toplevel(self.root)
        window.title("Clear memories")
        window.transient(self.root)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Tick what to clear. Hermes and Goose start again with nothing saved for what you "
                  "tick. Close Hermes and Goose first.", wraplength=460).pack(anchor="w")
        choices = {}
        for key, (title, about) in cfg.memory_parts().items():
            var = tk.BooleanVar(value=key not in ("rules", "goose_chats"))
            choices[key] = var
            ttk.Checkbutton(frame, text=title, variable=var).pack(anchor="w", pady=(8, 0))
            ttk.Label(frame, text="      " + about, wraplength=440, font=self.small).pack(anchor="w")
        keep = tk.BooleanVar(value=True)
        ttk.Separator(frame).pack(fill="x", pady=10)
        ttk.Checkbutton(frame, text="Keep a backup (recommended)", variable=keep).pack(anchor="w")
        ttk.Label(frame, text="      Files go to the memory-backups folder; the long-term memory is copied to a "
                  "backup memory in the same database. Untick to delete for good.", wraplength=440,
                  font=self.small).pack(anchor="w")
        ttk.Label(frame, text="Hermes's chats are deleted in the Hermes app.", wraplength=460,
                  font=self.small).pack(anchor="w", pady=(8, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(12, 0))

        def go():
            parts = [k for k, v in choices.items() if v.get()]
            if not parts:
                messagebox.showinfo("Clear memories", "Tick at least one.", parent=window)
                return
            still_open = self.running_apps()
            if still_open:
                messagebox.showwarning("Close first", "Close " + " and ".join(sorted(still_open)) +
                                       " first (also in the tray).", parent=window)
                return
            names = "\n".join("\u2022 " + cfg.memory_parts()[k][0] for k in parts)
            how = "A backup is kept." if keep.get() else "No backup: this can't be undone."
            if not messagebox.askyesno("Clear memories", f"Clear these?\n\n{names}\n\n{how}", parent=window,
                                       default="yes" if keep.get() else "no"):
                return
            window.destroy()
            self.run_in_background("Clearing memories", lambda: cfg.clear_memories(parts, keep.get()),
                                   lambda ok: messagebox.showinfo("Clear memories", "Done." if ok else
                                                                  "Some parts weren't cleared; see the box at the bottom."))
        ttk.Button(buttons, text="Clear", command=go).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(side="left", padx=6)

    # ---- updates ----------------------------------------------------------
    def build_updates_tab(self, frame):
        ttk.Label(frame, text="Checks Ollama, Hermes and its memory plugin, Goose, Git, Tesseract, Poppler, uv, "
                  "the Python packages and your Ollama models for newer versions. It only goes online when "
                  "you click the button, and nothing is installed until you tick it and click Update.",
                  wraplength=700).grid(row=0, column=0, columnspan=2, sticky="w")
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="w", pady=8)
        ttk.Button(buttons, text="Check for updates (goes online)", command=self.check_updates).pack(side="left")
        self.update_button = ttk.Button(buttons, text="Update selected", command=self.update_selected,
                                        state="disabled")
        self.update_button.pack(side="left", padx=6)
        self.update_list = ttk.Frame(frame)
        self.update_list.grid(row=2, column=0, columnspan=2, sticky="w")
        self.update_vars = []

    def check_updates(self):
        self.run_in_background("Checking for updates", updates.check_all, self.show_updates)

    def show_updates(self, result):
        for child in self.update_list.winfo_children():
            child.destroy()
        self.update_vars = []
        items, notes = result or ([], ["The check didn't finish; see the box at the bottom."])
        row = 0
        if not items:
            ttk.Label(self.update_list, text="Everything is up to date.", font=self.bold).grid(row=row, sticky="w")
            row += 1
        for entry in items:
            var = tk.BooleanVar(value=entry["default"])
            ttk.Checkbutton(self.update_list, variable=var,
                            text=(f"{entry['name']}: now {entry['current']}, new {entry['latest']}"
                                  if entry["current"] else f"{entry['name']}: {entry['latest']}")).grid(
                row=row, sticky="w")
            row += 1
            if entry["note"]:
                tk.Label(self.update_list, text="      " + entry["note"], fg="#1f5fa8", font=self.small,
                         wraplength=680, justify="left").grid(row=row, sticky="w")
                row += 1
            self.update_vars.append((entry, var))
        for note in notes:
            ttk.Label(self.update_list, text=note, font=self.small, wraplength=700).grid(row=row, sticky="w")
            row += 1
        self.update_button.configure(state="normal" if items else "disabled")

    def running_apps(self):
        if not WINDOWS:
            return set()
        listing = subprocess.run(["tasklist"], capture_output=True, text=True, errors="replace",
                                 creationflags=cfg.NO_WINDOW).stdout.lower()
        return {name for name, exe in (("Hermes", "hermes.exe"), ("Goose", "goose.exe")) if exe in listing}

    def update_selected(self):
        chosen = [entry for entry, var in self.update_vars if var.get()]
        if not chosen:
            messagebox.showinfo("Updates", "Tick at least one update first.")
            return
        kinds = {entry["update"][0] for entry in chosen}
        needs_closed = ({"Hermes"} if kinds & {"hermes", "hermes-plugin"} else set()) | \
                       ({"Goose"} if "goose" in kinds else set())
        still_open = self.running_apps() & needs_closed
        if still_open:
            messagebox.showwarning("Close first", "Close " + " and ".join(sorted(still_open)) +
                                   " first (also in the tray), then click Update selected again.")
            return
        text = "\n".join("\u2022 " + entry["name"] for entry in chosen)
        if not messagebox.askyesno("Update", f"Update these now?\n\n{text}\n\nThis needs the internet. "
                                   "Large models can take a long time to download."):
            return

        def done(ok):
            self.write("\nUpdates finished." if ok else "\nSome updates didn't work; see above.")
            self.saved = cfg.load_choices()
            messagebox.showinfo("Updates", "Done." if ok else "Some updates didn't work. The reason is in the "
                                "box at the bottom.")
        self.run_in_background("Updating", lambda: updates.update(chosen), done)

    # ---- buttons ----------------------------------------------------------
    def recommended(self):
        self.show_choices(cfg.DEFAULT_CHOICES)
        self.write("\nThe recommended settings are shown. Click \"Save and apply\" to use them.\n")

    def undo(self):
        self.show_choices(self.saved)
        self.write("\nShowing your saved settings again.\n")

    def warnings(self, c):
        notes = []
        if cfg.photo_model(c) != cfg.chat_model(c):
            notes.append("Photos go to a different model than the chat model, so the graphics card swaps "
                         "models for every photo (slower).")
        if cfg.memory_model(c) != cfg.chat_model(c):
            notes.append("Memories are saved by a different model than the chat model, so replies can "
                         "wait while the graphics card swaps models.")
        if cfg.memory_model(c) == "gemma4:e4b-128k":
            notes.append("gemma4:e4b-128k is less reliable at saving memories. Run \"Test memory\" "
                         "after saving.")
        if cfg.chat_model(c) in BIG or cfg.photo_model(c) in BIG:
            notes.append("qwen3.8:64k doesn't fit on a 12 GB card, so replies are several times slower.")
        if cfg.goose_model(c) != cfg.chat_model(c):
            notes.append("Goose uses a different model than Hermes, so switching between the two apps makes "
                         "the graphics card swap models.")
        if c["photo_model"] == "same" and c["chat_model"] not in SEEING:
            notes.append(f"{c['chat_model']} can't see photos. Pick a model under \"Looks at photos\".")
        return notes

    def missing_models(self, c):
        if self.installed is None:
            return []
        used = {cfg.chat_model(c), cfg.photo_model(c), cfg.memory_model(c), c["helper_model"], c["docs_ocr"],
                c["docs_vision"], c["docs_processor"], c["docs_reviewer"], cfg.goose_model(c)}
        return sorted(name for name in used if name in cfg.ALL_COPIES and name not in self.installed)

    def save(self):
        c = self.current()
        notes = self.warnings(c)
        if any(cfg.get_user_variable(k) != v for k, v in cfg.ollama_env(c).items()):
            notes.append("Ollama is closed and started again so it uses the new graphics card settings. "
                         "A reply that Hermes or Goose is writing right now stops. Let it finish first.")
        if notes and not messagebox.askyesno("Before saving", "\n\n".join(notes) + "\n\nSave anyway?"):
            return
        missing = self.missing_models(c)
        download = False
        if missing:
            sources = [cfg.ALL_COPIES[name][0] for name in missing]
            download = messagebox.askyesno(
                "Download models?",
                "These models aren't downloaded yet:\n\n" + "\n".join(sources) +
                "\n\nDownload them now? This can take a long time (qwen3.8 is about 17 GB)."
                "\n\nIf you choose No, the settings are saved anyway and Hermes can't use these models "
                "until they are downloaded.")

        def work():
            if download:
                ollama = cfg.find_ollama()
                for name in missing:
                    source = cfg.ALL_COPIES[name][0]
                    if source not in (cfg.installed_models() or set()):
                        print(f"Downloading {source} (this takes a while)...")
                        result = subprocess.run([ollama, "pull", source], capture_output=True, text=True,
                                                encoding="utf-8", errors="replace", creationflags=cfg.NO_WINDOW)
                        if result.returncode != 0:
                            print(f"  [X] Couldn't download {source}: {result.stderr.strip()[-300:]}")
                            continue
                        print(f"  [OK] {source} downloaded")
                    if name != cfg.chat_model(c):
                        cfg.make_copy(name, cfg.ALL_COPIES[name][1])
            return cfg.apply_choices(c)

        def done(changes):
            if not changes:
                messagebox.showwarning("Not saved", "Saving didn't finish. The reason is in the box at the bottom.")
                return
            self.saved = cfg.load_choices()
            self.installed = cfg.installed_models()
            self.write("\nSaved. Close Hermes and open it again so it uses the new settings.\n")
            if changes.get("memory") and WINDOWS:
                if messagebox.askyesno("Memory model changed",
                                       "The memory server is still using the old model. Stop it now? "
                                       "Hermes starts it again with the new model in your next chat.\n\n"
                                       "Close Hermes first if a reply is still being written."):
                    self.run_in_background("Stopping the memory server", cfg.stop_memory_server)
                    return
            messagebox.showinfo("Saved", "Saved.\n\nClose Hermes and open it again so it uses the new settings.")

        self.run_in_background("Saving and applying", work, done)

    def check(self):
        def work():
            cfg.RESULTS.clear()
            cfg.start_ollama()
            return cfg.cmd_check(None)
        self.run_in_background("Checking everything", work)

    def test_memory(self):
        if messagebox.askyesno("Test memory", "This saves a test fact, finds it again, saves a correction "
                               "and checks it. It takes 3-10 minutes and doesn't touch your own memories."
                               "\n\nStart the test?"):
            self.run_program("Memory test", [python_console(), str(script("memory_quick_test.py"))])

    def test_docs(self):
        if messagebox.askyesno("Test the document tool", "This makes three test images with known values, "
                               "reads them and checks the results. It takes about 5 minutes.\n\nStart the test?"):
            self.run_program("Document tool test", [python_console(), str(script("hermes_docs.py")), "selftest"])


def main():
    if WINDOWS:
        try:   # sharp text on high-resolution screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()

    def show_error(*args):
        messagebox.showerror("Local AI settings", "Something went wrong:\n\n" +
                             "".join(traceback.format_exception(*args))[-1500:])
    root.report_callback_exception = show_error
    SettingsApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            messagebox.showerror("Local AI settings", traceback.format_exc()[-1500:])
        except Exception:
            raise
