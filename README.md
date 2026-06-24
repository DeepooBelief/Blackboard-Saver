# Blackboard Saver

Blackboard Saver helps you download course materials from Imperial College Blackboard in bulk.
It opens Blackboard in a browser, lets you choose what kinds of files you want, and saves everything into tidy course folders on your computer.

## Windows exe release
A graphical interface for Windows users is available in https://github.com/DeepooBelief/Blackboard-Saver/releases. For Linux/Unix and MacOS users, we welcome forks to make the project compatible.

## Features

- Download course files from Blackboard without clicking through every page by hand.
- Choose which file types to download before scanning starts.
- Skip files above a maximum size you choose.
- Review skipped files later and manually keep anything important.
- View the original Blackboard page for any skipped file.
- Keep downloaded files organized by course and folder.
- Narrow the run to one course when you do not need everything.

## Requirements

- Windows 10 or above.
- Python 3.10 or newer.
- A supported desktop browser. Currently supports Chrome and Chromium-based browsers and Firefox.
- A compatible WebDriver. Selenium Manager can usually provide this automatically.
- Access to Imperial College Blackboard. You need to provide log-in credentials to your college microsoft account in a browser window.

Install Python dependencies:

```bash
pip install -r requirements.txt
```

Current Python package requirements:

```text
selenium==4.45.0
requests==2.34.2
```

## Quick Start

Run the downloader:

```bash
python blackboard_fully_parallel.py
```

Default UI workflow:

1. Choose launch options, download folder, and file filters in the startup window.
2. Accept the liability statement.
3. Click `Start login`.
4. Complete Blackboard login and MFA in the browser.
5. Click `Confirm and scan` in the login confirmation window.
6. Files that match the filters begin downloading as soon as they are found.
7. After scanning finishes, review filtered-out files and manually keep any you still want.

During scanning and downloading, the progress window shows activity logs and has an `Abort run` button. Aborting cancels queued scan/download work and waits for any active browser or download request to stop.

Downloaded files are saved to:

```text
~/Downloads/Blackboard
```

You can change the output folder in the startup window.

## Login

The normal UI flow does not require you to configure your Blackboard username or password in this project. On the first run, the downloader opens Blackboard in the selected browser. By default, browser detection uses the most recently accessed supported browser executable it can find. Sign in normally, finish MFA if required, then click `Confirm and scan` in the small confirmation window.

After a successful login, future runs will usually be able to continue without asking you to sign in again.

If login starts behaving strangely, remove the saved login file and run the script again for a fresh browser login.

The GUI asks for a download folder before login. Command-line runs without `--output-folder` may still ask for a folder in the terminal.

Each run writes a log named `run.YYMMDD.HHMMSS.log`. Packaged runs save it beside `BlackboardSaver.exe`; source runs save it in the current working directory. Use this log to find links reported as `Needs browser/manual handling`.

Do not commit saved login files or downloaded course material.

## Contributing

The maintainers are very lazy. If you fork this project, please do not expect us to review your commits.

## Command-Line Usage

Download only courses whose names contain a keyword:

```bash
python blackboard_fully_parallel.py --course "Machine Learning"
```

Run a dry scan without saving files:

```bash
python blackboard_fully_parallel.py --dry-run
```

Show scanner browser windows instead of running scanner browsers headlessly:

```bash
python blackboard_fully_parallel.py --show-scanners
```

Skip the UI and use command-line filters:

```bash
python blackboard_fully_parallel.py --no-ui --types pdf,docx,pptx --max-size-mb 200
```

Choose the output folder from the command line:

```bash
python blackboard_fully_parallel.py --output-folder "%USERPROFILE%\Downloads\Blackboard"
```

Review files when their type or size cannot be detected:

```bash
python blackboard_fully_parallel.py --exclude-unknown-types --exclude-unknown-size
```

Adjust how much work the downloader does at once:

```bash
python blackboard_fully_parallel.py --scan-workers 4 --download-workers 12
```

Choose a browser explicitly:

```bash
python blackboard_fully_parallel.py --browser chrome
```

## Options

| Option | Default | Description |
| --- | --- | --- |
| `--scan-workers` | `8` | Number of course pages to scan at once. |
| `--download-workers` | `16` | Number of files to download at once. |
| `--course` | all courses | Only scan courses whose name contains this text. |
| `--dry-run` | off | Print download tasks without writing files. |
| `--show-scanners` | off | Show scanner browser windows instead of headless scanners. |
| `--no-ui` | off | Skip login confirmation and review windows. |
| `--output-folder` | prompt/default | Folder where downloaded files are saved. |
| `--browser` | `auto` | Choose a browser. `auto` uses the most recently accessed supported browser executable. |
| `--types` | common document/archive/media types except small images | Comma-separated file extensions to download automatically. |
| `--max-size-mb` | unlimited | Maximum file size to download automatically. |
| `--exclude-unknown-types` | off | Put unknown file types into the review list. |
| `--exclude-unknown-size` | off | Put unknown file sizes into the review list. |

## Output Layout

Blackboard Saver creates folders based on course names and Blackboard content structure:

```text
Blackboard/
  ELEC60021 - Mathematics for Signals and Systems 2024-2025/
    Lecture Slides/
      Slides Session 1.pdf
      Slides Session 2.pdf
    Past Exam Papers/
      Exam paper 2023.pdf
```

If a filename already exists, the downloader appends a counter such as `(1)` to avoid overwriting files.

## Notes and Limitations

- This project is made for Imperial College Blackboard. Other schools may work after code changes, but that is not tested.
- Some Blackboard pages return HTML instead of direct file downloads. These are reported as `Needs browser/manual handling`.
- Blackboard may change over time. If scanning misses a section, use `--show-scanners` to see what the browser is doing.
- Very large Blackboard accounts may take a while to scan.

## Build Release

Build an unsigned single-file Windows executable from PowerShell:

```powershell
.\build_release.ps1
```

The release is written to:

```text
dist\BlackboardSaver.exe
dist\BlackboardSaver.exe.sha256
```

The build script creates a local build virtual environment, installs the app dependencies plus PyInstaller, cleans old build output, builds the executable, and runs a smoke check. Browsers are not bundled. The executable uses Selenium Manager for browser drivers when needed, so the first run may need network access.

When packaged, runtime cookies are stored in `.blackboard_saver_runtime` beside the executable and removed on normal app close. The executable is unsigned, so Windows may show a security warning.

## Development

Useful checks before committing:

```bash
python -m py_compile blackboard_fully_parallel.py
git diff --check
```

The main implementation lives in:

- `blackboard_fully_parallel.py`: UI, login, scanning, filtering, and downloading.
- `config.py`: download folder and fallback prompt helpers.
