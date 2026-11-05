<#
windows_setup.ps1 - Local AI on Windows: Ollama + Hermes Agent + Hindsight long-term memory +
the document tool that checks every value against the photo. Quality first, made for an
NVIDIA graphics card with 12 GB of memory and 64-128 GB of RAM.

HOW TO RUN: open PowerShell (NOT as administrator) in this folder and type:

    powershell -ExecutionPolicy Bypass -File .\windows_setup.ps1

It is safe to run again: what is already installed is kept, and settings files are backed up
before they change. Everything it prints is also saved in windows_setup_log.txt.

Options:
    -SkipModels      don't download models (for example when you copy them yourself)
    -NoDesktopApp    don't build the Hermes desktop app
    -NoGoose         don't install Goose (the second AI app)
    -SkipExtraModels don't download the two extra chat models (gemma4:e4b and qwen3.8,
                     about 27 GB together)
#>
param([switch]$SkipModels, [switch]$NoDesktopApp, [switch]$SkipExtraModels, [switch]$NoGoose)

$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
# Where the scripts are copied. Installs made before the rename keep using their old folder.
$AppDir = Join-Path $env:LOCALAPPDATA "local-ai-windows"
$OldAppDir = Join-Path $env:LOCALAPPDATA "locallinux"
if (-not (Test-Path $AppDir) -and (Test-Path $OldAppDir)) { $AppDir = $OldAppDir }
$HermesDir = Join-Path $env:LOCALAPPDATA "hermes"
$Log = Join-Path $Here "windows_setup_log.txt"
$Total = 10
try { Start-Transcript -Path $Log -Append | Out-Null } catch { }
# Offline mode blocks downloads; the setup needs them, so it is off while the setup runs.
# Step 8 turns it back on (if you chose it and the memory test has passed).
foreach ($name in "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "UV_OFFLINE") { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }

function Step($n, $title) {
    Write-Host ""
    Write-Host ("=" * 66)
    Write-Host " Step $n of ${Total}: $title"
    Write-Host ("=" * 66)
}
function Ok($text)   { Write-Host "  [OK] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "  [!]  $text" -ForegroundColor Yellow }
function Stop-Setup($text) {
    Write-Host "  [X]  $text" -ForegroundColor Red
    Write-Host "  Nothing is lost. Fix the problem above and run the script again."
    try { Stop-Transcript | Out-Null } catch { }
    exit 1
}
function Update-SessionPath {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Test-Command($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }
function Install-WithWinget($id, $name, $command) {
    if ($command -and (Test-Command $command)) { Ok "$name is installed"; return }
    $listed = (winget list --id $id -e --accept-source-agreements | Out-String)
    if ($listed -match [regex]::Escape($id)) { Ok "$name is installed"; return }
    Write-Host "  Installing $name..."
    winget install --id $id -e --silent --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        Warn "winget couldn't install $name (code $LASTEXITCODE). Install it later with: winget install $id"
    } else { Ok "$name installed" }
}
function Set-UserVariable($name, $value) {
    [Environment]::SetEnvironmentVariable($name, $value, "User")
    Set-Item -Path "Env:$name" -Value $value
}
function Invoke-Python {
    # Runs Python and passes its output on, so it is also saved in windows_setup_log.txt
    # (PowerShell 5 doesn't save the output of programs it runs directly).
    & $Python @args 2>&1 | ForEach-Object { "$_" }
}
function Wait-Ollama($seconds) {
    for ($i = 0; $i -lt $seconds; $i++) {
        try { Invoke-RestMethod "http://127.0.0.1:11434/api/version" -TimeoutSec 5 | Out-Null; return $true }
        catch { Start-Sleep -Seconds 1 }
    }
    return $false
}

Write-Host "Local AI on Windows: Ollama + Hermes Agent + Hindsight memory + document tool"

# --------------------------------------------------------------------------
Step 1 "Checking your system"
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
         ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($admin) { Stop-Setup "PowerShell is running as administrator. Open a normal PowerShell window and run it there." }
if (-not (Test-Command "winget")) { Stop-Setup "winget is missing. Install 'App Installer' from the Microsoft Store, then run this again." }
if (Test-Command "nvidia-smi") {
    Ok ("Graphics card: " + ((nvidia-smi --query-gpu=name,driver_version --format=csv,noheader) | Out-String).Trim())
} else {
    Warn "nvidia-smi wasn't found. Without the NVIDIA driver the models run on the CPU only (very slowly)."
}
foreach ($file in "hermes_docs.py", "windows_configure.py", "local_ai_rules.py", "memory_quick_test.py") {
    if (-not (Test-Path (Join-Path $Here $file))) { Stop-Setup "$file is missing. Put all files from the local-ai-windows download in this folder." }
}
Ok "All files are here."

# --------------------------------------------------------------------------
Step 2 "Programs (winget)"
Install-WithWinget "Ollama.Ollama" "Ollama" "ollama"
Install-WithWinget "Git.Git" "Git for Windows (Hermes runs its commands in Git Bash)" ""
Install-WithWinget "Python.Python.3.12" "Python 3.12" "py"
Install-WithWinget "UB-Mannheim.TesseractOCR" "Tesseract (turns photos upright)" ""
Install-WithWinget "oschwartz10612.Poppler" "Poppler (reads PDFs)" ""
Install-WithWinget "astral-sh.uv" "uv (starts Hindsight's memory server)" "uvx"
Update-SessionPath
$Python = $null
if (Test-Command "py") { $Python = ((py -3 -c "import sys; print(sys.executable)") | Out-String).Trim() }
elseif (Test-Command "python") { $Python = ((python -c "import sys; print(sys.executable)") | Out-String).Trim() }
if (-not $Python -or -not (Test-Path $Python)) { Stop-Setup "Python wasn't found. Close PowerShell, open a new one and run the script again." }
Ok "Python: $Python"
& $Python -m pip install --user --upgrade --quiet pyyaml pillow pillow-heif
if ($LASTEXITCODE -eq 0) { Ok "Python packages: pyyaml, pillow, pillow-heif (iPhone photos)" }
else { Warn "pip couldn't install the Python packages. Try: py -m pip install --user pyyaml pillow pillow-heif" }

# --------------------------------------------------------------------------
Step 3 "Ollama settings"
$OllamaSettings = [ordered]@{
    "OLLAMA_KEEP_ALIVE" = "30m";          # unload idle models after 30 minutes
    "OLLAMA_MAX_LOADED_MODELS" = "1";     # one model on the 12 GB card at a time
    "OLLAMA_NUM_PARALLEL" = "1";          # one request at a time
    "OLLAMA_FLASH_ATTENTION" = "1";
    "OLLAMA_GPU_OVERHEAD" = "1073741824"; # keep 1 GB of the card free (photos, the desktop)
    "GGML_CUDA_DISABLE_GRAPHS" = "1";     # avoids the out-of-memory crash when a reply starts
    "PYTHONUTF8" = "1";                   # Hindsight and Python tools crash on Windows without these
    "PYTHONIOENCODING" = "utf-8"
}
# Keep the choices made in the settings window (Local AI Settings)
$ChoicesFile = Join-Path $HermesDir "local_ai_choices.json"
if (Test-Path $ChoicesFile) {
    try {
        $choices = Get-Content $ChoicesFile -Raw | ConvertFrom-Json
        if ($choices.keep_alive) { $OllamaSettings["OLLAMA_KEEP_ALIVE"] = [string]$choices.keep_alive }
        if ($choices.gpu_free_mb) { $OllamaSettings["OLLAMA_GPU_OVERHEAD"] = [string]([int64]$choices.gpu_free_mb * 1048576) }
        Ok "Using your choices from the settings window ($ChoicesFile)"
    } catch { Warn "Couldn't read $ChoicesFile; using the recommended Ollama settings." }
}
foreach ($key in $OllamaSettings.Keys) { Set-UserVariable $key $OllamaSettings[$key] }
$GitBash = "C:\Program Files\Git\bin\bash.exe"
if (Test-Path $GitBash) { Set-UserVariable "HERMES_GIT_BASH_PATH" $GitBash; Ok "Hermes will use Git Bash: $GitBash" }
Ok ("Settings saved for your user (one model at a time, unused models unloaded after " +
    $OllamaSettings["OLLAMA_KEEP_ALIVE"] + ", flash attention).")
# Ollama reads these settings only when it starts, so a running Ollama is closed and started again.
$running = @(Get-Process "ollama app", "ollama" -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    Warn "Ollama is running. The setup now closes it and starts it again with the new settings."
    Warn "A reply that Hermes, Goose or another program is getting from Ollama right now will stop."
    Read-Host "  Let any reply finish, then press Enter to restart Ollama (or close this window to cancel)" | Out-Null
    $running | Stop-Process -Force
    # Its model runners (llama-server.exe in Ollama's folder) keep running when Ollama is stopped this way
    Get-Process "llama-server" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like (Join-Path $env:LOCALAPPDATA "Programs\Ollama\*") } | Stop-Process -Force
    Start-Sleep -Seconds 2
}
$OllamaApp = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama app.exe"
if (Test-Path $OllamaApp) { Start-Process $OllamaApp } else { Start-Process "ollama" -ArgumentList "serve" -WindowStyle Hidden }
if (-not (Wait-Ollama 60)) { Stop-Setup "Ollama didn't start. Start 'Ollama' from the Start menu, then run the script again." }
# Start Ollama with Windows (its installer normally does this; the shortcut can go missing)
$Startup = [Environment]::GetFolderPath("Startup")
$RunKey = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -ErrorAction SilentlyContinue |
           Out-String) -match "(?i)ollama"
if ((Test-Path $OllamaApp) -and -not $RunKey -and -not (Get-ChildItem $Startup -Filter "Ollama*.lnk" -ErrorAction SilentlyContinue)) {
    try {
        $link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $Startup "Ollama.lnk"))
        $link.TargetPath = $OllamaApp
        $link.WorkingDirectory = Split-Path $OllamaApp
        $link.Save()
        Ok "Ollama now starts with Windows"
    } catch { Warn "Couldn't make Ollama start with Windows. Start it from the Start menu when needed." }
} elseif (Test-Path $OllamaApp) { Ok "Ollama starts with Windows" }
Ok ("Ollama restarted with the new settings (version " + (Invoke-RestMethod "http://127.0.0.1:11434/api/version").version + ")")

# --------------------------------------------------------------------------
Step 4 "Downloading models (models you already have are kept)"
$installed = ((ollama list) | Out-String)
$Models = @("gemma4:26b", "nemotron-3.5-lightning", "qwen3-vl:8b", "glm-ocr")
# Extra chat models you can pick in Hermes or Goose (not used by the setup itself)
$ExtraModels = @("gemma4:e4b", "qwen3.8")
if (-not $SkipExtraModels) { $Models += $ExtraModels }
foreach ($model in $Models) {
    if ($installed -match ("(?m)^" + [regex]::Escape($model) + "(\s|:latest)")) { Ok "$model is already downloaded"; continue }
    if ($SkipModels) { Warn "$model is missing (skipped because of -SkipModels)"; continue }
    Write-Host "  Downloading $model..."
    ollama pull $model
    if ($LASTEXITCODE -ne 0) { Stop-Setup "Couldn't download $model. Check the internet connection and run the script again; finished downloads are kept." }
    Ok "$model is ready"
}

# --------------------------------------------------------------------------
Step 5 "Long-context copies of the models"
# Named after the model and its length (gemma4:26b-128k). A copy uses no extra disk space and the
# original model stays as it is. Copies with the old names (gemma4-hermes, ...) are renamed.
Invoke-Python (Join-Path $Here "windows_configure.py") copies
if ($LASTEXITCODE -ne 0) { Warn "Making the model copies had a problem (see above)." }

# --------------------------------------------------------------------------
Step 6 "Hermes Agent"
if (Test-Command "hermes") {
    Ok ("Hermes is installed: " + (Get-Command hermes).Source)
} else {
    # This downloads and runs the newest installer from the Hermes Agent project (Nous Research).
    # It is not pinned to a version: it installs whatever version that project publishes today.
    $HermesInstallerUrl = "https://hermes-agent.nousresearch.com/install.ps1"
    Write-Host "  Installing Hermes Agent with its official installer (several minutes)..."
    Write-Host "  The installer is downloaded from $HermesInstallerUrl and run as it is (newest version)."
    $installer = Invoke-RestMethod $HermesInstallerUrl
    if ($NoDesktopApp) { & ([scriptblock]::Create($installer)) -NonInteractive }
    else { & ([scriptblock]::Create($installer)) -NonInteractive -IncludeDesktop }
    Update-SessionPath
    if (Test-Command "hermes") { Ok "Hermes Agent installed" }
    else { Stop-Setup "Hermes didn't install. Close PowerShell, open a new one and run the script again." }
}

# --------------------------------------------------------------------------
Step 7 "Hindsight long-term memory"
if (Test-Path (Join-Path $HermesDir "plugins\hindsight")) { Ok "The Hindsight plugin is already installed" }
else {
    hermes plugins install hindsight
    if ($LASTEXITCODE -eq 0) { Ok "Hindsight plugin installed" } else { Warn "Installing the Hindsight plugin failed; run: hermes plugins install hindsight" }
}
Write-Host "  Tip: if Windows Defender reports the memory database (pg0), allow it, or add an"
Write-Host "  exclusion for $env:USERPROFILE\.hindsight in Windows Security."

# --------------------------------------------------------------------------
Step 8 "Goose desktop (a second AI app, with its own rules and memory)"
$GooseDir = Join-Path $env:LOCALAPPDATA "Programs\Goose"
$gooseExe = Get-ChildItem $GooseDir -Recurse -Filter "Goose.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($NoGoose) { Ok "Goose skipped (-NoGoose)" }
elseif ($gooseExe) { Ok ("Goose is installed: " + $gooseExe.FullName) }
else {
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $asset = $null
        foreach ($repo in "aaif-goose/goose", "block/goose") {
            try {
                $release = Invoke-RestMethod "https://api.github.com/repos/$repo/releases/tags/stable" -Headers @{ "User-Agent" = "local-ai-windows-setup" }
                # The desktop app is the win32 zip; the zips named ...pc-windows... are the command-line version.
                # Prefer the plain build; the -cuda one adds Goose's own model runner, which Ollama makes unneeded.
                $zips = @($release.assets | Where-Object { $_.name -match "(?i)win32.*\.zip$" })
                $asset = @($zips | Where-Object { $_.name -notmatch "(?i)cuda" }) + $zips | Select-Object -First 1
                if ($asset) { break }
            } catch { }
        }
        if (-not $asset) {   # GitHub's API can refuse many requests; the stable download link still works
            $asset = [pscustomobject]@{ name = "Goose-win32-x64.zip"; size = 0
                browser_download_url = "https://github.com/aaif-goose/goose/releases/download/stable/Goose-win32-x64.zip" }
        }
        Write-Host ("  Downloading " + $asset.name + " (a few hundred MB)...")
        $zip = Join-Path $env:TEMP $asset.name
        $oldProgress = $ProgressPreference; $ProgressPreference = "SilentlyContinue"   # much faster in PowerShell 5
        Invoke-WebRequest $asset.browser_download_url -OutFile $zip -UseBasicParsing
        $ProgressPreference = $oldProgress
        New-Item -ItemType Directory -Force -Path $GooseDir | Out-Null
        Expand-Archive $zip -DestinationPath $GooseDir -Force
        Remove-Item $zip -ErrorAction SilentlyContinue
        $gooseExe = Get-ChildItem $GooseDir -Recurse -Filter "Goose.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $gooseExe) { throw "Goose.exe wasn't in the download" }
        Ok ("Goose installed: " + $gooseExe.FullName)
    } catch {
        Warn "Couldn't install Goose ($_). Hermes is not affected. Download the Windows desktop zip from https://github.com/aaif-goose/goose/releases, extract it to $GooseDir and run the setup again."
    }
}
if ($gooseExe) {
    try {
        $link = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath("Programs")) "Goose.lnk"))
        $link.TargetPath = $gooseExe.FullName
        $link.WorkingDirectory = $gooseExe.DirectoryName
        $link.Description = "Goose: a second local AI app (Ollama), with its own rules and memory"
        $link.Save()
        Ok "Start menu: Goose"
    } catch { Warn "Couldn't make the Start menu shortcut for Goose." }
}

# --------------------------------------------------------------------------
Step 9 "Settings, rules and the document tool"
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
foreach ($file in "hermes_docs.py", "windows_configure.py", "local_ai_rules.py", "memory_quick_test.py",
                  "local_ai_settings.pyw", "local_ai_updates.py", "README.md") {
    $source = Join-Path $Here $file
    if (Test-Path $source) { Copy-Item $source $AppDir -Force }
}
Set-Content -Path (Join-Path $AppDir "hermes-docs.cmd") -Encoding ASCII `
    -Value "@`"$Python`" `"%~dp0hermes_docs.py`" %*"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$AppDir*") {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(";") + ";" + $AppDir), "User")
    Update-SessionPath
}
Ok "Files are in $AppDir (the hermes-docs command works in new PowerShell windows)"
# Start menu shortcut for the settings window
$PythonW = Join-Path (Split-Path $Python) "pythonw.exe"
$SettingsApp = Join-Path $AppDir "local_ai_settings.pyw"
if ((Test-Path $PythonW) -and (Test-Path $SettingsApp)) {
    try {
        $shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Local AI Settings.lnk"
        $link = (New-Object -ComObject WScript.Shell).CreateShortcut($shortcut)
        $link.TargetPath = $PythonW
        $link.Arguments = "`"$SettingsApp`""
        $link.WorkingDirectory = $AppDir
        $link.Description = "Change the Local AI settings (models, memory, document tool)"
        $link.Save()
        Ok "Start menu: Local AI Settings"
    } catch { Warn "Couldn't make the Start menu shortcut. Double-click $SettingsApp instead." }
}
Invoke-Python (Join-Path $AppDir "windows_configure.py") configure
if ($LASTEXITCODE -ne 0) { Stop-Setup "Writing the settings failed (see above)." }

# --------------------------------------------------------------------------
Step 10 "Checking everything"
foreach ($name in "HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "UV_OFFLINE") {   # what configure decided
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ($value) { Set-Item "Env:$name" $value } else { Remove-Item "Env:$name" -ErrorAction SilentlyContinue }
}
Invoke-Python (Join-Path $AppDir "windows_configure.py") check
$checkCode = $LASTEXITCODE

Write-Host ""
Write-Host ("=" * 66)
if ($checkCode -eq 0) { Write-Host " All done!" -ForegroundColor Green } else { Write-Host " Done, but something above is marked FAIL." -ForegroundColor Yellow }
Write-Host ("=" * 66)
Write-Host "  NEXT STEPS"
Write-Host "  1. Open Hermes from the Start menu (or type: hermes). Keep the model on gemma4:26b-128k."
Write-Host "  2. Drag photos into the chat, or name a folder: 'Look at the photos in Pictures\Receipts'."
Write-Host "  3. Quick memory test (3-10 minutes):   py `"$AppDir\memory_quick_test.py`""
Write-Host "  4. Test the document tool (5 minutes):  hermes-docs selftest"
Write-Host "  5. Check the settings again:            py `"$AppDir\windows_configure.py`" check"
Write-Host "  6. Change models and options: open 'Local AI Settings' from the Start menu."
Write-Host "  7. Goose (second app): open 'Goose' from the Start menu. If it asks for a provider, pick Ollama."
Write-Host "  The full output is in $Log"
try { Stop-Transcript | Out-Null } catch { }
exit $checkCode
