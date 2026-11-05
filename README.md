# Local AI for Windows

A one-click setup that installs a private AI assistant on your own Windows PC (Hermes Agent and Goose, running on Ollama), with long-term memory, rules it keeps, and a document reader that checks every value against the page.

> Screenshot: add `docs/screenshot.png` (placeholder line — leave it exactly like this for GUI apps)

## What it does
- Installs and connects **Ollama** (runs the AI models), **Hermes Agent** (the main assistant) and **Goose** (a second assistant with its own rules and memory).
- Gives Hermes long-term memory with **Hindsight**: it saves facts from each chat and finds them again in later chats.
- Lets both assistants keep rules: correct one and say "Make this a rule". Hermes writes the rule in `SOUL.md`, Goose in `.goosehints`, and both note what changed and why.
- Adds a document tool, **`hermes-docs`**, that reads photos, scans and PDFs with two reading models, pulls out the values, and checks each value against the text of its page.
- Adds a **Local AI Settings** window (Start menu) with a dropdown and an explanation for every setting, tests, Offline mode and an update checker.

## What you need
- Windows 10 or 11, signed in as a normal user (not "Run as administrator").
- An NVIDIA graphics card. The settings are made for a card with 12 GB of graphics memory and 64-128 GB of RAM.
- About 85 GB of free disk space for the models (about 55 GB without the extra chat models).
- An internet connection during the setup and the first memory test.
- winget ("App Installer" from the Microsoft Store; Windows 11 already has it).
- The setup installs the rest with winget: Ollama, Git for Windows, Python 3.12, Tesseract, Poppler and uv. It installs the Python packages in `requirements.txt` (`pyyaml`, `pillow`, `pillow-heif`) with pip.

## Install
1. Click **Code**, then **Download ZIP**, and unzip it, for example to `Downloads\local-ai-windows`.
2. Double-click **`SETUP_WINDOWS.bat`** in that folder. Do not use "Run as administrator". If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**.
3. If Ollama is already running, the setup stops and asks you to press Enter before it restarts Ollama.
4. Wait until it says **All done!** This takes a few minutes when Ollama and the models are already there, and up to a few hours when it downloads about 80 GB of models.

What the setup does, in 10 steps:
1. Checks the PC (not administrator, winget, the NVIDIA driver, all files present).
2. Installs what is missing with winget, and the Python packages with pip.
3. Saves Ollama's settings (one model at a time, 1 GB of graphics memory kept free, unused models unloaded after 30 minutes) and restarts Ollama.
4. Downloads the models you do not have yet. Models you already have are kept.
5. Makes long-context copies of the models (see **Settings and options**).
6. Installs Hermes Agent with its official Windows installer, including the desktop app.
7. Installs the Hindsight memory plugin.
8. Installs the Goose desktop app.
9. Copies the scripts to `%LOCALAPPDATA%\local-ai-windows` and writes every setting.
10. Checks every setting and prints PASS or FAIL for each.

Everything it prints is saved in `windows_setup_log.txt` next to the setup. Running it again is safe: what is installed is kept, your choices are kept, and settings files are backed up before they change.

Setup options (run in PowerShell, for example `powershell -ExecutionPolicy Bypass -File .\windows_setup.ps1 -NoGoose`):

| Option | What it does |
|---|---|
| `-SkipModels` | Does not download models. |
| `-SkipExtraModels` | Skips the two extra chat models (gemma4:e4b and qwen3.8, about 27 GB). |
| `-NoDesktopApp` | Installs only the Hermes command, not its desktop app. |
| `-NoGoose` | Does not install Goose. |

## How to use
1. Open **Hermes** from the Start menu and pick **gemma4:26b-128k** as the model.
2. Drag photos into the chat, or name a folder: "Look at the photos in Pictures\Receipts".
3. For exact text and numbers ("What is the total and date on each receipt?") Hermes uses the document tool. It takes about 1.5 minutes per photo. The results are saved in a `hermes_docs` folder next to the originals.
4. Tell it things to keep: "Remember that I file receipts by month." It saves the facts after its reply (about 30 seconds to a minute) and finds them in later chats.
5. When it does something wrong, correct it and say **"Make this a rule"**.
6. Goose works the same way. If it asks for a provider the first time, pick **Ollama**.

## Settings and options

### Local AI Settings
Open **Local AI Settings** from the Start menu. Every setting has a dropdown, a line about what it does, and a line about the selected choice. Settings marked **Safe to change** can be tried freely; settings marked **Usually leave as is** only need changing for a reason. Nothing changes until you click **Save and apply**. **Recommended settings** shows the recommended choices again, and **Undo changes** shows your saved choices again.

| Tab | What you can pick or do |
|---|---|
| Chat | The chat model, the conversation length, which model looks at photos, the helper model. |
| Memory | Which model saves memories; **Edit rules (SOUL.md)**; **Test memory (3-10 min)**; **Clear memories...** |
| Documents | **Quality** or **Balanced**, the models that read, find and review values; **Test the document tool (about 5 min)**. |
| Graphics card | How long an unused model stays loaded, how much memory is kept free; **Free the graphics card now**. |
| Offline | Offline mode (see below). |
| Goose | Goose's model and when it asks before it acts; **Edit Goose's rules (.goosehints)**. |
| Updates | **Check for updates (goes online)**, then tick what to update and click **Update selected**. |

Your choices are saved in `%LOCALAPPDATA%\hermes\local_ai_choices.json`.

### Models
Ollama gives a model a short conversation length unless told otherwise, so the setup makes a **copy** of each model named after the model and its length. A copy uses no extra disk space, and the original model stays as it is. In Hermes and Goose, pick the names that end in a length.

| Copy (pick this one) | Original model | Made by | Size | Used for |
|---|---|---|---|---|
| **gemma4:26b-128k** | gemma4:26b | Google | 18 GB | Chat (Hermes and Goose), looking at photos, saving memories, finding and reviewing values in documents |
| **nemotron-3.5-lightning:64k** | nemotron-3.5-lightning | NVIDIA | 25 GB | Hermes's side tasks (cannot see photos) |
| **qwen3-vl:8b-32k** | qwen3-vl:8b | Alibaba (Qwen) | 6.1 GB | Second reading of unclear photos in the document tool, spotting tables |
| **glm-ocr:16k** | glm-ocr | Zhipu AI | 2.7 GB | Main reading of the text in photos |
| **qwen3.8:64k** | qwen3.8 | Alibaba (Qwen) | 17 GB | Optional chat model: the most careful answers, several times slower |
| **gemma4:e4b-128k** | gemma4:e4b | Google | 9.6 GB | Optional chat model: small and quick, less careful, less reliable at saving memories |

The first four are used by default. Only one model is on the graphics card at a time. To switch the chat model, use **Local AI Settings** (Chat tab, **Chat model**), not only the Hermes model picker. Then the chat, photos and memory all use the same model, and the graphics card does not swap models after every reply.

### Offline mode
Offline mode is on by default. The memory server downloads its program and two small search models the first time it starts. After the memory test passes, Offline mode makes it use only those saved copies, and Hermes stops checking for updates. Chat, photos, documents, memory and rules then work without the internet. Web searches in Hermes still need it.

The setup and the update checker turn Offline mode off while they run. After a Hermes update it stays off until you run **Test memory** once with the internet on.

### Commands
```powershell
py "$env:LOCALAPPDATA\local-ai-windows\windows_configure.py" check      # checks every setting, changes nothing
py "$env:LOCALAPPDATA\local-ai-windows\memory_quick_test.py"            # long-term memory test, 3-10 minutes
py "$env:LOCALAPPDATA\local-ai-windows\local_ai_updates.py" check       # lists available updates
hermes-docs selftest                                                    # document tool test, about 5 minutes
hermes-docs forget "C:\Users\you\Pictures\Receipts"                     # removes a folder from the search index
```
If you installed an earlier version of this project, your scripts stay in their old folder, `%LOCALAPPDATA%\locallinux`, and keep working there. Use that folder in the commands above.

## Safety notes
- **What it installs:** the programs listed under **What you need**, the models, Hermes Agent, the Hindsight plugin and Goose.
- **The Hermes installer is downloaded live.** Step 6 downloads `https://hermes-agent.nousresearch.com/install.ps1` from the Hermes Agent project (Nous Research) and runs it. It is not pinned to a version, so you get whatever version that project publishes on the day you run the setup. Read that script first if you want to check it. Goose is downloaded from the `stable` release on GitHub (`aaif-goose/goose`).
- **Windows settings it changes:** user environment variables for Ollama, Offline mode and Hermes (no system-wide settings), your user `PATH` (adds the scripts folder), Start menu shortcuts, and a startup shortcut for Ollama if it has none.
- **Programs it closes:** the setup closes and restarts Ollama, but only after you press Enter. **Save and apply** in Local AI Settings also restarts Ollama when you change a graphics card setting, and tells you first. When the memory model changes, the settings window asks before it stops the memory server, and it only stops a process that is the Hindsight server.
- **Backups:** every settings file is copied to `<name>.backup-<date>` before it changes (the newest 5 are kept).
- **Clearing memories deletes data.** **Clear memories...** on the Memory tab lets you tick what to clear: Hermes's long-term memory, Hermes's own notes, Goose's memories, Goose's chat history, and the rules. It asks you to confirm first. With **Keep a backup** (on by default) the files are moved to `memory-backups` in the scripts folder, and the long-term memory is copied to a backup memory in the same database before it is cleared. Without a backup it cannot be undone. Close Hermes and Goose first.
- **What is sent:** after the setup, nothing about you leaves the PC. The memory server has no usage statistics, its trace sending is turned off (`HINDSIGHT_API_OTEL_TRACES_ENABLED=false`), its model connection points only at Ollama on this PC, and LiteLLM uses its built-in price list (`LITELLM_LOCAL_MODEL_COST_MAP=True`) instead of downloading one. **Check for updates** goes online only when you click it.
- **Privacy:** turn on BitLocker (Settings, then Privacy & security, then Device encryption) so your files and memories are encrypted if the drive is removed. People with administrator rights on the same PC can still read your files.

## Troubleshooting
| What you see | What to do |
|---|---|
| "Ollama answers: ... refused it" | Ollama is not running. Open **Ollama** from the Start menu and check again. |
| Hermes: `model '...' not found` | Click the model name in Hermes and pick **gemma4:26b-128k**. |
| A reply waits a long time | Make sure Hermes and Goose use the same model as the memory (gemma4:26b-128k). Different models make the graphics card swap models. |
| Windows Defender reports Hindsight's database | Allow it, or add an exclusion for `%USERPROFILE%\.hindsight`. |
| The memory test fails | Read `memory_quick_test_log.txt` in the scripts folder and the end of `%USERPROFILE%\.hindsight\profiles\hermes.log`. |

## How it works
`windows_setup.ps1` installs the programs with winget, sets Ollama's options as user environment variables, and pulls the models through Ollama's API. `windows_configure.py` writes the YAML and JSON settings for Hermes, Hindsight and Goose, creates the long-context model copies with Ollama Modelfiles, and checks everything afterwards. `hermes_docs.py` is a staged pipeline (prepare, read with an OCR model and a vision model, extract JSON, check every quote against its page, review doubtful values) that stores progress and a full-text search index in SQLite. `local_ai_settings.pyw` is a Tkinter window that runs the long jobs on a worker thread and shows their output.

### Files

| File | What it is |
|---|---|
| `SETUP_WINDOWS.bat` | Double-click to set everything up. |
| `windows_setup.ps1` | The setup itself (the .bat runs it). |
| `windows_configure.py` | Writes and checks all settings; used by the setup and the settings window. |
| `local_ai_rules.py` | The rules text for `SOUL.md`, the document skill text and the memory server settings. |
| `local_ai_settings.pyw` | The Local AI Settings window. |
| `local_ai_updates.py` | The update checker. |
| `hermes_docs.py` | The document tool (`hermes-docs`). |
| `memory_quick_test.py` | The quick long-term memory test. |

| What | Where on your PC |
|---|---|
| These scripts and `hermes-docs` | `%LOCALAPPDATA%\local-ai-windows` (older installs: `%LOCALAPPDATA%\locallinux`) |
| Hermes settings, rules (`SOUL.md`), choices | `%LOCALAPPDATA%\hermes` |
| Memory database and its log | `%USERPROFILE%\.hindsight` |
| Goose settings and rules (`.goosehints`) | `%APPDATA%\Block\goose\config` |
| Goose app | `%LOCALAPPDATA%\Programs\Goose` |

## License

MIT. See [LICENSE](LICENSE).
