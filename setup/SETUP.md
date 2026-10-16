# Auto Circuit Designer: Windows 10 setup

This sets up everything on your PC so Claude Desktop can design PCBs with you.
Versions were checked on 2026-10-16 and are listed in `versions.yaml`.

## Before you start

- Windows 10 with internet access. You will see a few "Do you want to allow this app to make changes" (UAC) prompts. Say yes; KiCad, Node.js and Java install for all users.
- Claude Desktop installed and signed in.
- Have this repository on your PC (for example `C:\Users\<you>\source\auto-circuit-designer`). Keep it where it is after setup, because Claude Desktop will run the companion from that folder.
- The first run downloads about 1.5 GB (mostly KiCad) and takes 10 to 20 minutes.

## Steps

1. Open PowerShell (not as administrator).
2. Go to the repository folder:
   `cd C:\Users\<you>\source\auto-circuit-designer`
3. Run the script:
   `powershell -ExecutionPolicy Bypass -File .\setup\setup-windows.ps1`
4. Answer two questions (press Enter to accept the default):
   - Projects folder: default `C:\Users\<you>\Documents\AutoCircuitDesigner\Projects`
   - Library folder: default `C:\Users\<you>\Documents\AutoCircuitDesigner\Library`
5. Approve the UAC prompts as they appear.
6. When it finishes, check the Summary table and the Smoke check lines. Every line should say `[ok]`.
7. Fully quit Claude Desktop (right-click the tray icon, Quit), then open it again.
8. If the script reports a problem, fix it using Troubleshooting below and run the script again. It skips anything already done.

Options: `-NoPrompt` (use defaults, no questions), `-ProjectsRoot` and `-LibraryRoot` (give the folders directly), `-KiCadVersion <x.y.z>` (install a different KiCad 10 patch), `-SkipConfigMerge` (do not touch the Claude Desktop config), `-InstallRoot <folder>` (install KiCad, Node.js, Java and git under that folder, for example `"D:\Program Files"`, using the official installers instead of winget; uv, Freerouting and the base MCP stay in your user profile).

## What gets installed (pinned versions)

| Item | Version | How |
|---|---|---|
| KiCad | 10.0.6 | winget `KiCad.KiCad`, else the official installer (signature checked) |
| Node.js LTS | 24.22.0 (any 18 or newer already installed is kept) | winget `OpenJS.NodeJS.LTS`, else the official MSI (SHA-256 checked) |
| Java, Eclipse Temurin JRE | 25.0.4.1 (any 25 or newer is kept) | winget `EclipseAdoptium.Temurin.25.JRE`, else the Temurin MSI (SHA-256 checked) |
| Freerouting | 2.4.1 | jar downloaded to `%LOCALAPPDATA%\acd\freerouting\` (SHA-256 checked) |
| Base MCP (mixelpixx/KiCAD-MCP-Server) | v2.7.0 | cloned to `%LOCALAPPDATA%\acd\kicad-mcp-server`, built with `npm ci` and `npm run build` |
| uv | 0.12.8 | winget `astral-sh.uv`, else the pinned install script |
| git | 2.55.0.3 | winget `Git.Git`, else the official installer |

Why Java 25: the Freerouting 2.4.1 jar was built for Java 25. The base MCP itself only needs Java 21 or newer, so 25 covers both.

## What the script changes on your PC

- Installs the programs above (only the ones missing).
- Creates `%LOCALAPPDATA%\acd\` with `freerouting\` and `kicad-mcp-server\` inside.
- Creates your Projects and Library folders. The library gets `symbols\acd.kicad_sym` (empty), `footprints\acd.pretty\`, `3dmodels\` and `registry.json` (`{}`). Existing files are never overwritten.
- Sets four user environment variables: `ACD_PROJECTS_ROOT`, `ACD_LIBRARY_ROOT`, `ACD_KICAD_CLI`, `FREEROUTING_JAR`.
- Runs `npm ci` and `npm run build` for the base MCP, and `pip install --user -r requirements.txt` into KiCad's own Python if its imports fail. The base project's own `setup-windows.ps1` is not used (it only finds KiCad under `C:\` and hides npm and pip errors).
- Runs `uv sync` in `companion\` (creates `companion\.venv`). This is skipped, with a note, if `companion\pyproject.toml` does not exist yet.
- Edits `%APPDATA%\Claude\claude_desktop_config.json`: adds or updates only the `kicad` and `acd` entries. Everything else in the file is kept. A copy is saved first as `claude_desktop_config.json.acd-backup-<date-time>` in the same folder. See `claude_desktop_config.example.json` for what the two entries look like.

The `kicad` entry sets the backend to `swig` (file based, works with KiCad closed) and points the base MCP at the Freerouting jar. The base MCP finds Freerouting through `FREEROUTING_JAR` and finds Java as the first `java` on PATH (it must be version 21 or newer). If another Java sits ahead of Java 25 on your PATH, the script puts the Java 25 folder first on PATH inside the `kicad` entry only.

The script does not touch KiCad's own library tables, does not create any PCB project, and does not commit anything to git.

## First test

After restarting Claude Desktop, open a new chat and ask: "List the tools from the kicad and acd servers." You should see both servers. If one is missing, open Settings, Developer in Claude Desktop and read that server's log.

## Undo

1. Claude Desktop config: quit Claude Desktop, open `%APPDATA%\Claude\`, delete `claude_desktop_config.json` and rename the newest `claude_desktop_config.json.acd-backup-...` file back to `claude_desktop_config.json`. (Or just delete the `kicad` and `acd` blocks in the file.)
2. Environment variables: in PowerShell run
   `foreach ($n in 'ACD_PROJECTS_ROOT','ACD_LIBRARY_ROOT','ACD_KICAD_CLI','FREEROUTING_JAR') { [Environment]::SetEnvironmentVariable($n, $null, 'User') }`
3. Downloaded tools: delete the folder `%LOCALAPPDATA%\acd\`. Delete `companion\.venv` in the repository.
4. Programs (only if you do not want them for anything else):
   `winget uninstall --id KiCad.KiCad --exact`
   `winget uninstall --id OpenJS.NodeJS.LTS --exact`
   `winget uninstall --id EclipseAdoptium.Temurin.25.JRE --exact`
   `winget uninstall --id astral-sh.uv --exact`
   `winget uninstall --id Git.Git --exact`
   Programs installed from a direct download can be removed in Settings, Apps.
5. Your Projects and Library folders are left alone. Delete them yourself only if they hold nothing you need.

## Troubleshooting

- **"running scripts is disabled"**: use the full command in step 3, which bypasses the policy for this one run only.
- **Newer KiCad 10 patch**: the pin is 10.0.6 (verified in winget). To move to a newer 10.0.x, run with `-KiCadVersion <x.y.z>` or edit `kicad.version` in `versions.yaml` and rerun.
- **The pcbnew import check fails**: the base MCP's swig backend uses KiCad's `pcbnew` Python module. The base project's code says swig is deprecated and "will be removed in KiCAD 10.0". If KiCad 10.0.x on your PC has no working `pcbnew`, the `kicad` server cannot run in swig mode. Do not work around it; tell Claude, because the architecture (backend choice) then needs a decision. This is the one thing that could not be checked before you ran the script.
- **Node, Java or uv "not found on PATH" right after installing**: open a new PowerShell window and run the script again.
- **Claude Desktop installed from the Microsoft Store**: its config may live under `%LOCALAPPDATA%\Packages\Claude_...\LocalCache\Roaming\Claude\`. The script looks there when `%APPDATA%\Claude` does not exist. If your servers do not appear, check which folder Claude Desktop actually reads (Settings, Developer, Edit Config opens the right one).
- **Windows 10 support**: Microsoft ended support for Windows 10 on 2025-10-14, and KiCad no longer tests on it. It should work. A KiCad bug has to be reproduced on a supported system before KiCad will fix it.

## Changing a pinned version later

Edit `versions.yaml` (and the matching checksum or commit), run the script again, and test with a throwaway project before you rely on it. The base MCP check refuses to build if its tag points at a different commit than the one pinned.
