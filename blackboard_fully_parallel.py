import argparse
import ast
import atexit
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from selenium import webdriver
from selenium.common.exceptions import ElementClickInterceptedException, ElementNotInteractableException, NoSuchElementException
from selenium.webdriver.common.by import By

from config import DEFAULT_ROOT_DIR, get_email, get_password, get_root_dir


HOST = "https://bb.imperial.ac.uk/webapps/portal/execute/tabs/tabAction?tab_tab_group_id=_1_1"
HOST_BASE = "https://bb.imperial.ac.uk/"
HOST_WITHOUT_REDIRECT = "https://bb.imperial.ac.uk/auth-saml/logout/"
COOKIE_PATH = Path("cookies.txt")
RUNTIME_DIR_NAME = ".blackboard_saver_runtime"
LIABILITY_TEXT = (
    "I understand that I am responsible for using this tool only with content I am authorized to access, "
    "and I accept all liability for using it."
)
SKIP_NAME_PARTS = ("animation",)
STOP = object()
VIDEO_HINTS = (
    "video",
    "recording",
    "lecturecast",
    "panopto",
    "echo360",
    "stream",
    ".mp4",
    ".mov",
    ".m4v",
    ".webm",
)
KNOWN_FILE_EXTENSIONS = {
    ".7z",
    ".csv",
    ".doc",
    ".docx",
    ".gif",
    ".htm",
    ".html",
    ".jpeg",
    ".jpg",
    ".m",
    ".m4v",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".ppt",
    ".pptx",
    ".py",
    ".rar",
    ".txt",
    ".webm",
    ".xls",
    ".xlsx",
    ".zip",
}
CONTENT_TYPE_EXTENSIONS = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/zip": ".zip",
    "text/csv": ".csv",
    "text/plain": ".txt",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/mp4": ".mp4",
    "video/x-m4v": ".m4v",
}
DEFAULT_EXCLUDED_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png"}
DEFAULT_ALLOWED_EXTENSIONS = KNOWN_FILE_EXTENSIONS - DEFAULT_EXCLUDED_EXTENSIONS


@dataclass(frozen=True)
class DownloadTask:
    url: str
    label: str
    directory: Path
    source: str
    page_url: str = ""


@dataclass(frozen=True)
class DownloadCandidate:
    task: DownloadTask
    filename: str
    extension: str
    size_bytes: int | None
    content_type: str = ""
    resolved_url: str = ""
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DownloadResult:
    task: DownloadTask
    status: str
    path: Path | None = None
    detail: str = ""


@dataclass
class Stats:
    found: int = 0
    filtered: int = 0
    downloaded: int = 0
    needs_browser: int = 0
    failed: int = 0
    seen_urls: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)


print_lock = threading.Lock()
progress_log_queue = None
run_log_file = None
run_log_path = None


@dataclass(frozen=True)
class FilterOptions:
    allowed_extensions: frozenset[str] = frozenset(DEFAULT_ALLOWED_EXTENSIONS)
    max_size_bytes: int | None = None
    keep_unknown_types: bool = True
    keep_unknown_size: bool = True


@dataclass(frozen=True)
class BrowserSpec:
    name: str
    label: str
    driver: str
    executable_names: tuple[str, ...]
    path_commands: tuple[str, ...]
    windows_dirs: tuple[str, ...]
    mac_apps: tuple[str, ...]
    linux_paths: tuple[str, ...]


@dataclass(frozen=True)
class BrowserConfig:
    name: str
    label: str
    driver: str
    executable: Path | None = None


BROWSER_SPECS = (
    BrowserSpec(
        name="firefox",
        label="Firefox",
        driver="firefox",
        executable_names=("firefox.exe", "firefox"),
        path_commands=("firefox", "firefox.exe", "firefox-beta", "firefox-developer-edition", "firefox-nightly"),
        windows_dirs=(
            r"Mozilla Firefox",
            r"Mozilla Firefox Beta",
            r"Firefox Developer Edition",
            r"Mozilla Firefox Developer Edition",
            r"Firefox Nightly",
            r"Mozilla Firefox Nightly",
        ),
        mac_apps=("Firefox.app", "Firefox Beta.app", "Firefox Developer Edition.app", "Firefox Nightly.app"),
        linux_paths=(
            "/usr/bin/firefox",
            "/usr/local/bin/firefox",
            "/snap/bin/firefox",
            "/opt/firefox/firefox",
            "/opt/firefox-beta/firefox",
            "/opt/firefox-developer-edition/firefox",
            "/opt/firefox-nightly/firefox",
            "/usr/lib/firefox/firefox",
            "/usr/lib/firefox-developer-edition/firefox",
        ),
    ),
    BrowserSpec(
        name="chrome",
        label="Chrome",
        driver="chrome",
        executable_names=("chrome.exe", "chrome", "google-chrome", "google-chrome-stable"),
        path_commands=("chrome", "chrome.exe", "google-chrome", "google-chrome-stable"),
        windows_dirs=(r"Google\Chrome\Application", r"Google\Chrome Beta\Application", r"Google\Chrome Dev\Application"),
        mac_apps=("Google Chrome.app", "Google Chrome Beta.app", "Google Chrome Dev.app", "Google Chrome Canary.app"),
        linux_paths=(
            "/usr/bin/google-chrome",
            "/usr/bin/google-chrome-stable",
            "/usr/bin/google-chrome-beta",
            "/usr/bin/google-chrome-unstable",
            "/opt/google/chrome/chrome",
        ),
    ),
    BrowserSpec(
        name="edge",
        label="Edge",
        driver="edge",
        executable_names=("msedge.exe", "msedge", "microsoft-edge", "microsoft-edge-stable"),
        path_commands=("msedge", "msedge.exe", "microsoft-edge", "microsoft-edge-stable"),
        windows_dirs=(
            r"Microsoft\Edge\Application",
            r"Microsoft\Edge Beta\Application",
            r"Microsoft\Edge Dev\Application",
            r"Microsoft\Edge SxS\Application",
        ),
        mac_apps=("Microsoft Edge.app", "Microsoft Edge Beta.app", "Microsoft Edge Dev.app", "Microsoft Edge Canary.app"),
        linux_paths=(
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/microsoft-edge-beta",
            "/usr/bin/microsoft-edge-dev",
            "/opt/microsoft/msedge/msedge",
        ),
    ),
    BrowserSpec(
        name="brave",
        label="Brave",
        driver="chrome",
        executable_names=("brave.exe", "brave", "brave-browser", "brave-browser-stable"),
        path_commands=("brave", "brave.exe", "brave-browser", "brave-browser-stable"),
        windows_dirs=(
            r"BraveSoftware\Brave-Browser\Application",
            r"BraveSoftware\Brave-Browser-Beta\Application",
            r"BraveSoftware\Brave-Browser-Dev\Application",
            r"BraveSoftware\Brave-Browser-Nightly\Application",
        ),
        mac_apps=("Brave Browser.app", "Brave Browser Beta.app", "Brave Browser Dev.app", "Brave Browser Nightly.app"),
        linux_paths=(
            "/usr/bin/brave",
            "/usr/bin/brave-browser",
            "/usr/bin/brave-browser-stable",
            "/usr/bin/brave-browser-beta",
            "/usr/bin/brave-browser-dev",
            "/opt/brave.com/brave/brave-browser",
        ),
    ),
    BrowserSpec(
        name="chromium",
        label="Chromium",
        driver="chrome",
        executable_names=("chromium.exe", "chromium", "chromium-browser"),
        path_commands=("chromium", "chromium-browser", "chromium.exe"),
        windows_dirs=(r"Chromium\Application", r"Chromium"),
        mac_apps=("Chromium.app",),
        linux_paths=(
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
            "/usr/lib/chromium/chromium",
        ),
    ),
)
SUPPORTED_BROWSERS = ("auto",) + tuple(spec.name for spec in BROWSER_SPECS)
BROWSER_SPECS_BY_NAME = {spec.name: spec for spec in BROWSER_SPECS}


@dataclass(frozen=True)
class RunOptions:
    browser: str = "auto"
    scan_workers: int = 8
    download_workers: int = 16
    course: str | None = None
    dry_run: bool = False
    show_scanners: bool = False
    no_ui: bool = False
    output_folder: Path | None = None
    filters: FilterOptions = field(default_factory=FilterOptions)
    gui_mode: bool = False


@dataclass(frozen=True)
class RuntimePaths:
    runtime_dir: Path | None
    cookie_path: Path


def log(message):
    with print_lock:
        message = str(message)
        if progress_log_queue is not None:
            try:
                progress_log_queue.put(message)
            except Exception:
                pass
        try:
            print(message, flush=True)
        except (AttributeError, OSError, RuntimeError, ValueError):
            pass
        if run_log_file is not None:
            try:
                run_log_file.write(f"{message}\n")
                run_log_file.flush()
            except Exception:
                pass


def is_frozen_build():
    return bool(getattr(sys, "frozen", False))


def run_base_dir():
    if is_frozen_build():
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def init_run_log():
    global run_log_file, run_log_path
    if run_log_file is not None:
        return run_log_path

    timestamp = time.strftime("%y%m%d.%H%M%S")
    path = run_base_dir() / f"run.{timestamp}.log"
    run_log_file = path.open("a", encoding="utf-8", buffering=1)
    run_log_path = path
    atexit.register(close_run_log)
    log(f"Run log: {path}")
    return path


def close_run_log():
    global run_log_file
    if run_log_file is None:
        return
    try:
        run_log_file.close()
    except Exception:
        pass
    run_log_file = None


def cleanup_runtime_dir(runtime_dir):
    if not runtime_dir:
        return
    try:
        shutil.rmtree(runtime_dir, ignore_errors=True)
    except Exception:
        pass


def prepare_runtime_paths():
    if not is_frozen_build():
        return RuntimePaths(runtime_dir=None, cookie_path=COOKIE_PATH)

    exe_dir = Path(sys.executable).resolve().parent
    runtime_dir = exe_dir / RUNTIME_DIR_NAME
    try:
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        runtime_dir.mkdir(parents=True, exist_ok=False)
        probe = runtime_dir / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        cleanup_runtime_dir(runtime_dir)
        raise RuntimeError(
            f"Could not create runtime folder next to the executable: {runtime_dir}\n\n{exc}"
        ) from exc

    atexit.register(cleanup_runtime_dir, runtime_dir)
    return RuntimePaths(runtime_dir=runtime_dir, cookie_path=runtime_dir / COOKIE_PATH.name)


def show_gui_message(kind, title, message):
    try:
        import tkinter as tk
        from tkinter import messagebox
    except Exception:
        log(f"{title}: {message}")
        return

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    if kind == "error":
        messagebox.showerror(title, message, parent=root)
    else:
        messagebox.showinfo(title, message, parent=root)
    root.destroy()


def report_error(options, title, message):
    log(message)
    if options.gui_mode:
        show_gui_message("error", title, message)


def _path_key(path):
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    key = str(resolved)
    return key.lower() if os.name == "nt" else key


def _add_browser_candidate(candidates, spec, path):
    if not path:
        return
    path = Path(os.path.expandvars(str(path))).expanduser()
    if path.is_file():
        candidates[(spec.name, _path_key(path))] = BrowserConfig(spec.name, spec.label, spec.driver, path)


def _registry_path(value):
    value = str(value or "").strip()
    if not value:
        return None
    value = os.path.expandvars(value)
    if value.startswith('"'):
        end = value.find('"', 1)
        raw = value[1:end] if end != -1 else value.strip('"')
    else:
        raw = value.split(",", 1)[0].strip()
    return Path(raw) if raw else None


def _windows_registry_browser_paths(spec):
    if os.name != "nt":
        return

    try:
        import winreg
    except ImportError:
        return

    def read_string(key, name):
        try:
            if name is None:
                return str(winreg.QueryValue(key, None) or "")
            value, _kind = winreg.QueryValueEx(key, name)
        except OSError:
            return ""
        return str(value or "")

    windows_executable_names = {name.lower() for name in spec.executable_names if name.lower().endswith(".exe")}
    for executable_name in windows_executable_names:
        app_path_keys = (
            (winreg.HKEY_CURRENT_USER, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{executable_name}"),
        )
        for hive, subkey in app_path_keys:
            try:
                with winreg.OpenKey(hive, subkey) as key:
                    yield _registry_path(read_string(key, None))
            except OSError:
                pass

    uninstall_roots = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    display_keywords = (spec.name.lower(), spec.label.lower())
    for hive, root in uninstall_roots:
        try:
            with winreg.OpenKey(hive, root) as root_key:
                count = winreg.QueryInfoKey(root_key)[0]
                for index in range(count):
                    try:
                        with winreg.OpenKey(root_key, winreg.EnumKey(root_key, index)) as app_key:
                            display_name = read_string(app_key, "DisplayName").lower()
                            if not any(keyword in display_name for keyword in display_keywords):
                                continue

                            install_location = read_string(app_key, "InstallLocation")
                            if install_location:
                                for executable_name in spec.executable_names:
                                    yield Path(os.path.expandvars(install_location)) / executable_name

                            display_icon = _registry_path(read_string(app_key, "DisplayIcon"))
                            if display_icon and display_icon.name.lower() in windows_executable_names:
                                yield display_icon
                    except OSError:
                        continue
        except OSError:
            continue


def _windows_common_browser_paths(spec):
    roots = []
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        value = os.getenv(env_name)
        if value:
            roots.append(Path(value))

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "Programs")

    for root in roots:
        if not root.exists():
            continue
        for dirname in spec.windows_dirs:
            for executable_name in spec.executable_names:
                yield root / dirname / executable_name

        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if child.is_dir() and spec.name in child.name.lower():
                for executable_name in spec.executable_names:
                    yield child / executable_name


def _mac_browser_paths(spec):
    for root in (Path("/Applications"), Path.home() / "Applications"):
        for app_name in spec.mac_apps:
            executable_dir = root / app_name / "Contents" / "MacOS"
            yield executable_dir / Path(app_name).stem
            for executable_name in spec.executable_names:
                yield executable_dir / executable_name


def _browser_recency_key(browser_config):
    path = browser_config.executable
    if path is None:
        return (0.0, 0.0, browser_config.label.lower())
    try:
        stat = path.stat()
    except OSError:
        return (0.0, 0.0, str(path).lower())
    return (stat.st_atime, stat.st_mtime, str(path).lower())


def find_browser_candidates(requested_browser="auto"):
    specs = BROWSER_SPECS if requested_browser == "auto" else (BROWSER_SPECS_BY_NAME[requested_browser],)
    candidates = {}

    for spec in specs:
        _add_browser_candidate(candidates, spec, os.getenv(f"BLACKBOARD_{spec.name.upper()}_BINARY"))

        for command in spec.path_commands:
            _add_browser_candidate(candidates, spec, shutil.which(command))

        if os.name == "nt":
            for path in _windows_registry_browser_paths(spec) or ():
                _add_browser_candidate(candidates, spec, path)
            for path in _windows_common_browser_paths(spec):
                _add_browser_candidate(candidates, spec, path)
        elif sys.platform == "darwin":
            for path in _mac_browser_paths(spec):
                _add_browser_candidate(candidates, spec, path)
        else:
            for path in spec.linux_paths:
                _add_browser_candidate(candidates, spec, path)

    browser_configs = list(candidates.values())
    browser_configs.sort(key=_browser_recency_key, reverse=True)
    return browser_configs


def resolve_browser(requested_browser):
    requested_browser = (requested_browser or "auto").lower()
    if requested_browser not in SUPPORTED_BROWSERS:
        raise RuntimeError(f"Unsupported browser {requested_browser!r}. Use one of: {', '.join(SUPPORTED_BROWSERS)}.")

    candidates = find_browser_candidates(requested_browser)
    if candidates:
        selected = candidates[0]
        log(f"Using browser: {selected.label}")
        return selected

    if requested_browser == "auto":
        raise RuntimeError("No supported browser executable was found.")

    spec = BROWSER_SPECS_BY_NAME[requested_browser]
    log(f"Using browser: {spec.label}")
    return BrowserConfig(spec.name, spec.label, spec.driver)


def parse_extensions(value):
    extensions = set()
    for part in (value or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        if not part.startswith("."):
            part = f".{part}"
        extensions.add(part)
    return frozenset(extensions)


def parse_content_length(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_size(size_bytes):
    if size_bytes is None:
        return "unknown"
    units = ("B", "KB", "MB", "GB")
    value = float(size_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def inspect_task(task, cookies):
    session = make_session(cookies)
    response = None
    try:
        try:
            response = session.head(task.url, allow_redirects=True, timeout=30)
            if response.status_code >= 400 or not response.headers:
                response.close()
                response = None
        except requests.RequestException:
            response = None

        if response is None:
            response = session.get(task.url, allow_redirects=True, stream=True, timeout=30)

        content_type = response.headers.get("content-type", "").lower()
        server_name = filename_from_response(response, task.label)
        filename = final_name(task.label, server_name, content_type)
        extension = Path(filename).suffix.lower() or extension_from_content_type(content_type)
        size_bytes = parse_content_length(response.headers.get("content-length"))
        resolved_url = response.url
    except Exception:
        filename = validate_title(task.label)
        extension = Path(filename).suffix.lower()
        size_bytes = None
        content_type = ""
        resolved_url = task.url
    finally:
        if response is not None:
            response.close()
        session.close()

    return DownloadCandidate(
        task=task,
        filename=filename,
        extension=extension,
        size_bytes=size_bytes,
        content_type=content_type,
        resolved_url=resolved_url,
    )


def filter_candidate(candidate, filters):
    reasons = []
    extension = candidate.extension.lower()

    if extension:
        if extension not in filters.allowed_extensions:
            reasons.append(f"type {extension} not selected")
    elif not filters.keep_unknown_types:
        reasons.append("file type unknown")

    if filters.max_size_bytes is not None:
        if candidate.size_bytes is None:
            if not filters.keep_unknown_size:
                reasons.append("file size unknown")
        elif candidate.size_bytes > filters.max_size_bytes:
            reasons.append(f"larger than {format_size(filters.max_size_bytes)}")

    if not reasons:
        return candidate
    return DownloadCandidate(
        task=candidate.task,
        filename=candidate.filename,
        extension=candidate.extension,
        size_bytes=candidate.size_bytes,
        content_type=candidate.content_type,
        resolved_url=candidate.resolved_url,
        reasons=tuple(reasons),
    )


class TaskCollector:
    def __init__(self, cookies, filters, stats, download_queue):
        self.cookies = cookies
        self.filters = filters
        self.stats = stats
        self.download_queue = download_queue
        self.accepted = []
        self.rejected = []
        self.lock = threading.Lock()

    def add(self, url, label, directory, source, page_url):
        if not url or should_skip_name(label):
            return

        task = DownloadTask(
            url=url,
            label=validate_title(label),
            directory=directory,
            source=source,
            page_url=page_url or "",
        )
        with self.stats.lock:
            if task.url in self.stats.seen_urls:
                return
            self.stats.seen_urls.add(task.url)
            self.stats.found += 1
            found = self.stats.found

        candidate = filter_candidate(inspect_task(task, self.cookies), self.filters)
        with self.lock:
            if candidate.reasons:
                self.rejected.append(candidate)
                with self.stats.lock:
                    self.stats.filtered += 1
                log(
                    f"[filtered {found}] {task.directory / candidate.filename} "
                    f"({'; '.join(candidate.reasons)})"
                )
            else:
                self.accepted.append(candidate)
                log(f"[found {found}] {task.directory / candidate.filename}")
                self.download_queue.put(candidate.task)


def isloaded(driver, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if driver.execute_script("return document.readyState == 'complete';"):
            return
        time.sleep(0.25)
    raise TimeoutError("Timed out waiting for page load")


def read_cookie_file(path):
    raw = path.read_text(encoding="utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return ast.literal_eval(raw)


def save_cookie(driver, path):
    path.write_text(json.dumps(driver.get_cookies(), indent=2), encoding="utf-8")


def load_cookie(driver, path):
    add_cookies(driver, read_cookie_file(path))


def add_cookies(driver, cookies):
    for cookie in cookies:
        cookie = dict(cookie)
        cookie.pop("sameSite", None)
        try:
            driver.add_cookie(cookie)
        except Exception as exc:
            if cookie.get("domain"):
                domainless_cookie = dict(cookie)
                domainless_cookie.pop("domain", None)
                try:
                    driver.add_cookie(domainless_cookie)
                    continue
                except Exception:
                    pass
            log(f"Skipped cookie {cookie.get('name')}: {exc}")


def validate_title(title):
    title = re.sub(r"[\/\\\:\*\?\"\<\>\|]", "_", title.strip())
    title = re.sub(r"\s+", " ", title)
    return title or "untitled"


def should_skip_name(name):
    lower_name = name.lower()
    return any(part in lower_name for part in SKIP_NAME_PARTS)


def looks_like_video(text, href):
    if "#contextMenu" in href or href.endswith("#close"):
        return False
    if "/webapps/blackboard/content/listContent.jsp" in href:
        return False

    value = f"{text} {href}".lower()
    if text.strip().lower() == "video" and "bbcswebdav" not in href.lower():
        return False

    return any(hint in value for hint in VIDEO_HINTS)


def xpath_literal(value):
    if '"' not in value:
        return f'"{value}"'
    if "'" not in value:
        return f"'{value}'"
    parts = value.split('"')
    return "concat(" + ', \'"\', '.join(f'"{part}"' for part in parts) + ")"


def click_when_visible(driver, element):
    driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
    time.sleep(0.2)
    try:
        element.click()
    except (ElementClickInterceptedException, ElementNotInteractableException):
        driver.execute_script("arguments[0].click();", element)


def find_visible_link_by_text(driver, texts):
    for text in texts:
        candidates = driver.find_elements(
            By.XPATH,
            f"//*[normalize-space()={xpath_literal(text)}]/ancestor-or-self::a[1]",
        )
        for candidate in candidates:
            if candidate.is_displayed() and candidate.is_enabled():
                return candidate
    return None


class ToolTip:
    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self.after_id = None
        self.window = None
        widget.bind("<Enter>", self.schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def schedule(self, _event=None):
        self.cancel()
        self.after_id = self.widget.after(self.delay, self.show)

    def cancel(self):
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None

    def show(self):
        if self.window or not self.text:
            return
        import tkinter as tk

        x = self.widget.winfo_rootx() + 24
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self.window,
            text=self.text,
            justify="left",
            background="#ffffe0",
            relief="solid",
            borderwidth=1,
            padx=6,
            pady=4,
            wraplength=360,
        )
        label.pack()

    def hide(self, _event=None):
        self.cancel()
        if self.window:
            self.window.destroy()
            self.window = None


class ProgressWindow:
    def __init__(self, cancel_event=None, title="Blackboard Saver"):
        import tkinter as tk
        from tkinter import ttk

        self.log_queue = queue.Queue()
        self.cancel_event = cancel_event
        self.closed = False
        self.line_count = 0

        self.root = tk.Tk()
        self.root.title(title)
        self.root.geometry("780x420")
        self.root.minsize(560, 300)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close_attempt)

        main = ttk.Frame(self.root, padding=14)
        main.pack(fill="both", expand=True)
        main.rowconfigure(1, weight=1)
        main.columnconfigure(0, weight=1)

        ttk.Label(main, text="Blackboard Saver is running. Leave this window open.").grid(
            row=0,
            column=0,
            sticky="w",
            pady=(0, 8),
        )

        log_frame = ttk.Frame(main)
        log_frame.grid(row=1, column=0, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.text = tk.Text(log_frame, height=12, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scrollbar.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        footer = ttk.Frame(main)
        footer.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)

        self.status_var = tk.StringVar(value="Starting...")
        ttk.Label(footer, textvariable=self.status_var).grid(row=0, column=0, sticky="w")
        self.abort_button = ttk.Button(footer, text="Abort run", command=self.abort)
        self.abort_button.grid(row=0, column=1, sticky="e", padx=(8, 0))
        if self.cancel_event is None:
            self.abort_button.state(["disabled"])

        self.install()
        self.pump()

    def install(self):
        global progress_log_queue
        progress_log_queue = self.log_queue

    def on_close_attempt(self):
        self.status_var.set("The run is still active. This window will close when the run finishes.")

    def abort(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.abort_button.state(["disabled"])
        self.status_var.set("Abort requested. Stopping workers...")
        log("Abort requested by user.")

    def aborted(self):
        return self.cancel_event is not None and self.cancel_event.is_set()

    def set_status(self, message):
        self.status_var.set(message)
        self.pump()

    def pump(self):
        if self.closed:
            return
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.text.configure(state="normal")
                self.text.insert("end", f"{message}\n")
                self.line_count += 1
                while self.line_count > 600:
                    self.text.delete("1.0", "2.0")
                    self.line_count -= 1
                self.text.configure(state="disabled")
                self.text.see("end")
        except queue.Empty:
            pass

        try:
            self.root.update_idletasks()
            self.root.update()
        except Exception:
            self.closed = True

    def close(self):
        global progress_log_queue
        if progress_log_queue is self.log_queue:
            progress_log_queue = None
        if self.closed:
            return
        self.closed = True
        try:
            self.root.destroy()
        except Exception:
            pass


def show_launch_options_ui(defaults):
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
    except Exception as exc:
        log(f"Could not open launch UI ({exc}).")
        return None

    result = {"options": None}
    tooltips = []

    root = tk.Tk()
    root.title("Blackboard Saver")
    root.geometry("920x760")
    root.minsize(760, 480)

    shell = ttk.Frame(root, padding=16)
    shell.pack(fill="both", expand=True)
    shell.columnconfigure(0, weight=1)
    shell.rowconfigure(0, weight=1)

    canvas = tk.Canvas(shell, highlightthickness=0)
    scrollbar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
    scroll_frame = ttk.Frame(canvas)
    canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    def resize_scroll_frame(event):
        canvas.itemconfigure(canvas_window, width=event.width)

    def update_scroll_region(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))

    def on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    scroll_frame.bind("<Configure>", update_scroll_region)
    canvas.bind("<Configure>", resize_scroll_frame)
    canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_mousewheel))
    canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
    canvas.grid(row=0, column=0, sticky="nsew")
    scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))

    main = ttk.Frame(scroll_frame)
    main.pack(fill="both", expand=True)

    ttk.Label(main, text="Blackboard Saver").pack(anchor="w")

    launch_frame = ttk.LabelFrame(main, text="Launch options", padding=12)
    launch_frame.pack(fill="x", pady=(12, 8))
    launch_frame.columnconfigure(1, weight=1)

    browser_var = tk.StringVar(value=defaults.browser)
    course_var = tk.StringVar(value=defaults.course or "")
    output_var = tk.StringVar(value=str(defaults.output_folder or DEFAULT_ROOT_DIR))
    scan_workers_var = tk.StringVar(value=str(defaults.scan_workers))
    download_workers_var = tk.StringVar(value=str(defaults.download_workers))
    dry_run_var = tk.BooleanVar(value=defaults.dry_run)
    show_scanners_var = tk.BooleanVar(value=defaults.show_scanners)

    def attach_tooltip(widget, text):
        tooltips.append(ToolTip(widget, text))
        return widget

    ttk.Label(launch_frame, text="Browser").grid(row=0, column=0, sticky="w", pady=3)
    browser_combo = ttk.Combobox(
        launch_frame,
        textvariable=browser_var,
        values=SUPPORTED_BROWSERS,
        state="readonly",
        width=18,
    )
    browser_combo.grid(row=0, column=1, sticky="w", pady=3)
    attach_tooltip(browser_combo, "auto uses the most recently accessed supported browser executable.")

    ttk.Label(launch_frame, text="Course contains").grid(row=1, column=0, sticky="w", pady=3)
    course_entry = ttk.Entry(launch_frame, textvariable=course_var)
    course_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=3)
    attach_tooltip(course_entry, "Leave blank to scan every visible course.")

    ttk.Label(launch_frame, text="Download folder").grid(row=2, column=0, sticky="w", pady=3)
    output_entry = ttk.Entry(launch_frame, textvariable=output_var)
    output_entry.grid(row=2, column=1, sticky="ew", pady=3)

    def browse_output_folder():
        selected = filedialog.askdirectory(initialdir=output_var.get() or str(DEFAULT_ROOT_DIR), parent=root)
        if selected:
            output_var.set(selected)

    browse_button = ttk.Button(launch_frame, text="Browse", command=browse_output_folder)
    browse_button.grid(row=2, column=2, sticky="e", padx=(8, 0), pady=3)
    attach_tooltip(output_entry, "Files are saved under this folder, grouped by course.")
    attach_tooltip(browse_button, "Choose where downloaded Blackboard files should be saved.")

    ttk.Label(launch_frame, text="Scan workers").grid(row=3, column=0, sticky="w", pady=3)
    scan_entry = ttk.Entry(launch_frame, textvariable=scan_workers_var, width=10)
    scan_entry.grid(row=3, column=1, sticky="w", pady=3)
    attach_tooltip(scan_entry, "Number of browser workers used while scanning course pages.")

    ttk.Label(launch_frame, text="Download workers").grid(row=4, column=0, sticky="w", pady=3)
    download_entry = ttk.Entry(launch_frame, textvariable=download_workers_var, width=10)
    download_entry.grid(row=4, column=1, sticky="w", pady=3)
    attach_tooltip(download_entry, "Number of HTTP workers used while saving files.")

    dry_run_check = ttk.Checkbutton(launch_frame, text="Dry run", variable=dry_run_var)
    dry_run_check.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 3))
    attach_tooltip(dry_run_check, "Scan and print planned downloads without saving files.")

    show_scanners_check = ttk.Checkbutton(launch_frame, text="Show scanner browser windows", variable=show_scanners_var)
    show_scanners_check.grid(row=6, column=0, columnspan=2, sticky="w", pady=3)
    attach_tooltip(show_scanners_check, "Keep scanner browsers visible instead of running them headlessly.")

    type_frame = ttk.LabelFrame(main, text="File types", padding=12)
    type_frame.pack(fill="both", expand=True, pady=8)

    type_vars = {}
    for index, extension in enumerate(sorted(KNOWN_FILE_EXTENSIONS)):
        var = tk.BooleanVar(value=extension in defaults.filters.allowed_extensions)
        type_vars[extension] = var
        row = index // 4
        column = index % 4
        ttk.Checkbutton(type_frame, text=extension, variable=var).grid(
            row=row,
            column=column,
            sticky="w",
            padx=(0, 24),
            pady=3,
        )

    def set_all_types(value):
        for var in type_vars.values():
            var.set(value)

    type_buttons = ttk.Frame(main)
    type_buttons.pack(fill="x", pady=(0, 8))
    ttk.Button(type_buttons, text="Select all", command=lambda: set_all_types(True)).pack(side="left")
    ttk.Button(type_buttons, text="Select none", command=lambda: set_all_types(False)).pack(side="left", padx=(8, 0))
    ttk.Button(
        type_buttons,
        text="Default",
        command=lambda: [var.set(ext in DEFAULT_ALLOWED_EXTENSIONS) for ext, var in type_vars.items()],
    ).pack(side="left", padx=(8, 0))

    limits_frame = ttk.LabelFrame(main, text="Limits", padding=12)
    limits_frame.pack(fill="x", pady=(0, 8))
    limits_frame.columnconfigure(1, weight=1)

    max_size_var = tk.StringVar(
        value="" if defaults.filters.max_size_bytes is None else f"{defaults.filters.max_size_bytes / (1024 * 1024):g}"
    )
    keep_unknown_types_var = tk.BooleanVar(value=defaults.filters.keep_unknown_types)
    keep_unknown_size_var = tk.BooleanVar(value=defaults.filters.keep_unknown_size)

    ttk.Label(limits_frame, text="Maximum file size (MB)").grid(row=0, column=0, sticky="w", pady=3)
    max_size_entry = ttk.Entry(limits_frame, textvariable=max_size_var, width=14)
    max_size_entry.grid(row=0, column=1, sticky="w", padx=(12, 0), pady=3)
    attach_tooltip(max_size_entry, "Leave blank for no size limit.")

    unknown_types_check = ttk.Checkbutton(
        limits_frame,
        text="Keep files with unknown type",
        variable=keep_unknown_types_var,
    )
    unknown_types_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 3))
    attach_tooltip(unknown_types_check, "When unchecked, unknown file types go to the review list.")

    unknown_size_check = ttk.Checkbutton(
        limits_frame,
        text="Keep files with unknown size",
        variable=keep_unknown_size_var,
    )
    unknown_size_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=3)
    attach_tooltip(unknown_size_check, "When unchecked, unknown file sizes go to the review list.")

    footer = ttk.Frame(shell)
    footer.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(12, 0))
    footer.columnconfigure(0, weight=1)

    liability_var = tk.BooleanVar(value=False)
    liability_check = ttk.Checkbutton(footer, text=LIABILITY_TEXT, variable=liability_var)
    liability_check.grid(row=0, column=0, sticky="ew")

    status_var = tk.StringVar()
    ttk.Label(footer, textvariable=status_var).grid(row=1, column=0, sticky="w", pady=(4, 0))

    actions = ttk.Frame(footer)
    actions.grid(row=2, column=0, sticky="ew", pady=(10, 0))
    start_button = ttk.Button(actions, text="Start login")
    start_button.pack(side="right")
    ttk.Button(actions, text="Cancel", command=root.destroy).pack(side="right", padx=(0, 8))

    def validation_error():
        if not liability_var.get():
            return "Accept the liability statement to continue."
        if not output_var.get().strip():
            return "Choose a download folder."
        for label, value in (
            ("Scan workers", scan_workers_var.get()),
            ("Download workers", download_workers_var.get()),
        ):
            try:
                if int(value) < 1:
                    return f"{label} must be at least 1."
            except ValueError:
                return f"{label} must be a whole number."
        raw_max_size = max_size_var.get().strip()
        if raw_max_size:
            try:
                if float(raw_max_size) < 0:
                    return "Maximum file size must be positive or blank."
            except ValueError:
                return "Maximum file size must be a number or blank."
        return ""

    def update_start_state(*_args):
        error = validation_error()
        status_var.set(error)
        if error:
            start_button.state(["disabled"])
        else:
            start_button.state(["!disabled"])

    def build_options():
        error = validation_error()
        if error:
            messagebox.showerror("Invalid launch options", error, parent=root)
            return None

        raw_max_size = max_size_var.get().strip()
        max_size_bytes = None
        if raw_max_size:
            max_size_bytes = int(float(raw_max_size) * 1024 * 1024)

        filters = FilterOptions(
            allowed_extensions=frozenset(ext for ext, var in type_vars.items() if var.get()),
            max_size_bytes=max_size_bytes,
            keep_unknown_types=keep_unknown_types_var.get(),
            keep_unknown_size=keep_unknown_size_var.get(),
        )
        return RunOptions(
            browser=browser_var.get(),
            scan_workers=int(scan_workers_var.get()),
            download_workers=int(download_workers_var.get()),
            course=course_var.get().strip() or None,
            dry_run=dry_run_var.get(),
            show_scanners=show_scanners_var.get(),
            no_ui=False,
            output_folder=Path(output_var.get()).expanduser(),
            filters=filters,
            gui_mode=True,
        )

    def start():
        options = build_options()
        if options is None:
            return
        result["options"] = options
        root.destroy()

    start_button.configure(command=start)
    root.protocol("WM_DELETE_WINDOW", root.destroy)

    for var in (
        browser_var,
        course_var,
        output_var,
        scan_workers_var,
        download_workers_var,
        dry_run_var,
        show_scanners_var,
        max_size_var,
        keep_unknown_types_var,
        keep_unknown_size_var,
        liability_var,
    ):
        var.trace_add("write", update_start_state)
    update_start_state()

    root.mainloop()
    return result["options"]


def show_filter_options_ui(defaults):
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except Exception as exc:
        log(f"Could not open filter UI ({exc}); continuing with command-line/default filters.")
        return defaults

    result = {"options": None}
    root = tk.Tk()
    root.title("Blackboard Saver filters")
    root.geometry("760x620")
    root.minsize(680, 520)

    main = ttk.Frame(root, padding=16)
    main.pack(fill="both", expand=True)

    ttk.Label(main, text="Choose what to download before scanning").pack(anchor="w")

    type_frame = ttk.LabelFrame(main, text="File types", padding=12)
    type_frame.pack(fill="both", expand=True, pady=(12, 8))

    type_vars = {}
    extensions = sorted(KNOWN_FILE_EXTENSIONS)
    for index, extension in enumerate(extensions):
        var = tk.BooleanVar(value=extension in defaults.allowed_extensions)
        type_vars[extension] = var
        row = index // 4
        column = index % 4
        ttk.Checkbutton(type_frame, text=extension, variable=var).grid(
            row=row,
            column=column,
            sticky="w",
            padx=(0, 24),
            pady=3,
        )

    def set_all(value):
        for var in type_vars.values():
            var.set(value)

    button_row = ttk.Frame(main)
    button_row.pack(fill="x", pady=(0, 8))
    ttk.Button(button_row, text="Select all", command=lambda: set_all(True)).pack(side="left")
    ttk.Button(button_row, text="Select none", command=lambda: set_all(False)).pack(side="left", padx=(8, 0))
    ttk.Button(
        button_row,
        text="Default",
        command=lambda: [var.set(ext in DEFAULT_ALLOWED_EXTENSIONS) for ext, var in type_vars.items()],
    ).pack(side="left", padx=(8, 0))

    limits_frame = ttk.LabelFrame(main, text="Limits", padding=12)
    limits_frame.pack(fill="x")

    ttk.Label(limits_frame, text="Maximum file size (MB, blank for no limit)").grid(row=0, column=0, sticky="w")
    max_size_var = tk.StringVar(
        value="" if defaults.max_size_bytes is None else f"{defaults.max_size_bytes / (1024 * 1024):g}"
    )
    ttk.Entry(limits_frame, textvariable=max_size_var, width=16).grid(row=0, column=1, sticky="w", padx=(12, 0))

    keep_unknown_types_var = tk.BooleanVar(value=defaults.keep_unknown_types)
    keep_unknown_size_var = tk.BooleanVar(value=defaults.keep_unknown_size)
    ttk.Checkbutton(
        limits_frame,
        text="Keep files with unknown type",
        variable=keep_unknown_types_var,
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
    ttk.Checkbutton(
        limits_frame,
        text="Keep files with unknown size",
        variable=keep_unknown_size_var,
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

    action_row = ttk.Frame(main)
    action_row.pack(fill="x", pady=(14, 0))

    def start():
        raw_max_size = max_size_var.get().strip()
        max_size_bytes = None
        if raw_max_size:
            try:
                max_size_mb = float(raw_max_size)
                if max_size_mb < 0:
                    raise ValueError
                max_size_bytes = int(max_size_mb * 1024 * 1024)
            except ValueError:
                messagebox.showerror("Invalid size", "Maximum file size must be a positive number or blank.")
                return

        selected = frozenset(ext for ext, var in type_vars.items() if var.get())
        result["options"] = FilterOptions(
            allowed_extensions=selected,
            max_size_bytes=max_size_bytes,
            keep_unknown_types=keep_unknown_types_var.get(),
            keep_unknown_size=keep_unknown_size_var.get(),
        )
        root.destroy()

    def cancel():
        root.destroy()

    ttk.Button(action_row, text="Start scanning", command=start).pack(side="right")
    ttk.Button(action_row, text="Cancel", command=cancel).pack(side="right", padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.mainloop()
    return result["options"]


def review_filtered_candidates_ui(candidates):
    if not candidates:
        return []

    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        log(f"Could not open review UI ({exc}); filtered files will be skipped.")
        return []

    selected = {"candidates": []}
    root = tk.Tk()
    root.title("Review filtered Blackboard files")
    root.geometry("1120x720")
    root.minsize(900, 520)

    main = ttk.Frame(root, padding=12)
    main.pack(fill="both", expand=True)

    ttk.Label(
        main,
        text="Filtered files are unchecked. Tick any file you still want to download.",
    ).pack(anchor="w")

    toolbar = ttk.Frame(main)
    toolbar.pack(fill="x", pady=(8, 8))

    canvas = tk.Canvas(main, highlightthickness=0)
    scrollbar = ttk.Scrollbar(main, orient="vertical", command=canvas.yview)
    rows_frame = ttk.Frame(canvas)
    rows_frame.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas_window = canvas.create_window((0, 0), window=rows_frame, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)

    def resize_canvas(event):
        canvas.itemconfigure(canvas_window, width=event.width)

    canvas.bind("<Configure>", resize_canvas)
    canvas.pack(side="left", fill="both", expand=True)
    scrollbar.pack(side="right", fill="y")

    def on_mousewheel(event):
        canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    canvas.bind_all("<MouseWheel>", on_mousewheel)

    headers = ("Keep", "File", "Type", "Size", "Reason", "Blackboard page")
    widths = (7, 45, 9, 12, 30, 18)
    for column, (header, width) in enumerate(zip(headers, widths)):
        ttk.Label(rows_frame, text=header, width=width).grid(row=0, column=column, sticky="w", padx=4, pady=(0, 6))

    vars_by_candidate = []
    for row, candidate in enumerate(candidates, start=1):
        keep_var = tk.BooleanVar(value=False)
        vars_by_candidate.append((keep_var, candidate))

        ttk.Checkbutton(rows_frame, variable=keep_var).grid(row=row, column=0, sticky="w", padx=4, pady=2)
        ttk.Label(rows_frame, text=candidate.filename, width=45).grid(row=row, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(rows_frame, text=candidate.extension or "unknown", width=9).grid(
            row=row,
            column=2,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Label(rows_frame, text=format_size(candidate.size_bytes), width=12).grid(
            row=row,
            column=3,
            sticky="w",
            padx=4,
            pady=2,
        )
        ttk.Label(rows_frame, text="; ".join(candidate.reasons), width=30).grid(
            row=row,
            column=4,
            sticky="w",
            padx=4,
            pady=2,
        )

        page_url = candidate.task.page_url or candidate.task.url
        ttk.Button(
            rows_frame,
            text="Open page",
            command=lambda url=page_url: webbrowser.open(url),
            width=14,
        ).grid(row=row, column=5, sticky="w", padx=4, pady=2)

    def set_all(value):
        for var, _candidate in vars_by_candidate:
            var.set(value)

    def continue_download():
        selected["candidates"] = [candidate for var, candidate in vars_by_candidate if var.get()]
        root.destroy()

    ttk.Button(toolbar, text="Keep all", command=lambda: set_all(True)).pack(side="left")
    ttk.Button(toolbar, text="Keep none", command=lambda: set_all(False)).pack(side="left", padx=(8, 0))
    ttk.Label(toolbar, text=f"{len(candidates)} filtered file(s)").pack(side="left", padx=(16, 0))
    ttk.Button(toolbar, text="Continue", command=continue_download).pack(side="right")

    root.protocol("WM_DELETE_WINDOW", continue_download)
    root.mainloop()
    return selected["candidates"]


def wait_for_login_confirmation_ui():
    try:
        import tkinter as tk
        from tkinter import ttk
    except Exception as exc:
        log(f"Could not open login confirmation UI ({exc}); falling back to terminal confirmation.")
        input("Finish any MFA/browser login prompts, then press Enter here...")
        return True

    result = {"confirmed": False}
    root = tk.Tk()
    root.title("Blackboard login")
    root.geometry("520x180")
    root.minsize(460, 160)
    root.attributes("-topmost", True)

    main = ttk.Frame(root, padding=18)
    main.pack(fill="both", expand=True)

    ttk.Label(main, text="Finish logging in to Blackboard in the browser.").pack(anchor="w")
    ttk.Label(main, text="After the Blackboard course page is visible, click Confirm to start scanning.").pack(
        anchor="w",
        pady=(8, 0),
    )

    actions = ttk.Frame(main)
    actions.pack(fill="x", pady=(22, 0))

    def confirm():
        result["confirmed"] = True
        root.destroy()

    def cancel():
        root.destroy()

    ttk.Button(actions, text="Confirm and scan", command=confirm).pack(side="right")
    ttk.Button(actions, text="Cancel", command=cancel).pack(side="right", padx=(0, 8))

    root.protocol("WM_DELETE_WINDOW", cancel)
    root.after(800, lambda: root.attributes("-topmost", False))
    root.mainloop()
    return result["confirmed"]


def make_driver(browser_config, headless=False):
    if browser_config.driver == "firefox":
        options = webdriver.FirefoxOptions()
        if browser_config.executable:
            options.binary_location = str(browser_config.executable)
        options.set_preference("browser.download.useDownloadDir", True)
        options.set_preference("browser.download.folderList", 2)
        options.set_preference("browser.download.manager.showWhenStarting", False)
        options.set_preference("pdfjs.disabled", True)
        if headless:
            options.add_argument("-headless")

        driver = webdriver.Firefox(options=options)
        if headless:
            driver.set_window_size(1920, 1080)
        else:
            driver.maximize_window()
        driver.implicitly_wait(2)
        return driver

    if browser_config.driver == "edge":
        options = webdriver.EdgeOptions()
    else:
        options = webdriver.ChromeOptions()

    if browser_config.executable:
        options.binary_location = str(browser_config.executable)
    options.add_argument("--start-maximized")
    options.add_experimental_option(
        "prefs",
        {
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "plugins.always_open_pdf_externally": True,
        },
    )
    if headless:
        options.add_argument("--headless=new")
        options.add_argument("--window-size=1920,1080")

    if browser_config.driver == "edge":
        driver = webdriver.Edge(options=options)
    else:
        driver = webdriver.Chrome(options=options)

    if not headless:
        driver.maximize_window()
    driver.implicitly_wait(2)
    return driver


def login(driver, cookie_path, use_ui=True):
    if cookie_path.exists():
        driver.get(HOST_WITHOUT_REDIRECT)
        isloaded(driver)
        load_cookie(driver, cookie_path)
        driver.get(HOST)
        isloaded(driver)
        return

    if use_ui:
        driver.get(HOST)
        isloaded(driver)
        if not wait_for_login_confirmation_ui():
            raise RuntimeError("Login cancelled.")
        driver.get(HOST)
        isloaded(driver)
        save_cookie(driver, cookie_path)
        return

    email = get_email()
    password = get_password()
    driver.get(HOST)
    isloaded(driver)
    driver.find_element(By.XPATH, '//*[@id="i0116"]').send_keys(email)
    driver.find_element(By.XPATH, '//*[@id="idSIButton9"]').click()
    time.sleep(2)
    driver.find_element(By.XPATH, '//*[@id="i0118"]').send_keys(password)
    time.sleep(0.5)
    driver.find_element(By.XPATH, '//*[@id="idSIButton9"]').click()
    time.sleep(0.5)
    input("Finish any MFA/browser login prompts, then press Enter here...")
    driver.get(HOST)
    isloaded(driver)
    save_cookie(driver, cookie_path)


def get_course_links(driver):
    courses = []
    links = driver.find_elements(By.XPATH, '//*[@id="_4_1termCourses_noterm"]/ul/*/a')
    for link in links:
        name = validate_title(link.text)
        href = link.get_attribute("href")
        if name and href:
            courses.append((name, href))
    return courses


def prepare_scanner_driver(cookies, browser_config, headless):
    driver = make_driver(browser_config, headless=headless)
    driver.get(HOST_WITHOUT_REDIRECT)
    isloaded(driver)
    add_cookies(driver, cookies)
    driver.get(HOST)
    isloaded(driver)
    return driver


def enqueue_task(collector, url, label, directory, source, page_url):
    collector.add(url, label, directory, source, page_url)


def collect_video_links(collector, driver, directory, page_url):
    links = driver.execute_script(
        """
        return Array.from(document.querySelectorAll('a[href]')).map((link) => ({
            href: link.href,
            text: (link.innerText || link.textContent || link.getAttribute('title') || link.href || '').trim()
        }));
        """
    )
    for link in links:
        label = validate_title(link.get("text") or "video")
        href = link.get("href")
        if href and looks_like_video(label, href):
            enqueue_task(collector, href, label, directory, "video-link", page_url)


def collect_from_table(collector, table, base_dir, page_url):
    rows = table.find_elements(By.XPATH, "./tbody/tr")
    columns = []
    for row_index, row in enumerate(rows):
        cells = row.find_elements(By.XPATH, "./td")
        if row_index == 0:
            columns = [validate_title(cell.text) for cell in cells]
            continue
        if not cells:
            continue

        row_dir = base_dir / validate_title(cells[0].text)
        for cell_index, cell in enumerate(cells[1:], start=1):
            column_name = columns[cell_index] if cell_index < len(columns) else f"Column {cell_index}"
            cell_dir = row_dir / validate_title(column_name)
            for file_link in cell.find_elements(By.XPATH, ".//a[@href]"):
                href = file_link.get_attribute("href")
                if href and "ant-x" not in href:
                    label = file_link.text or Path(urlparse(href).path).name
                    enqueue_task(collector, href, label, cell_dir, "table", page_url)


def collect_content(driver, collector, directory, cancel_event=None, depth=1):
    if cancel_event is not None and cancel_event.is_set():
        return
    directory.mkdir(parents=True, exist_ok=True)
    page_url = driver.current_url
    collect_video_links(collector, driver, directory, page_url)
    items = driver.find_elements(By.CLASS_NAME, "item_icon")
    log(f"{'  ' * (depth - 1)}Scanning {len(items)} items in {directory}")

    for index in range(len(items)):
        if cancel_event is not None and cancel_event.is_set():
            return
        items = driver.find_elements(By.CLASS_NAME, "item_icon")
        if index >= len(items):
            break

        item = items[index]
        item_type = (item.get_attribute("alt") or "").strip()
        item_type_lower = item_type.lower()

        if item_type_lower == "file":
            try:
                link = item.find_element(By.XPATH, './../*/h3/a')
            except NoSuchElementException:
                log(f"{'  ' * depth}Could not find file link for item {index + 1}")
                continue
            enqueue_task(collector, link.get_attribute("href"), link.text, directory, "file", page_url)

        elif item_type_lower == "content folder":
            try:
                folder_heading = item.find_element(By.XPATH, './../*/h3')
            except NoSuchElementException:
                log(f"{'  ' * depth}Could not find folder heading for item {index + 1}")
                continue

            folder_name = validate_title(folder_heading.text)
            log(f"{'  ' * depth}{folder_name}/")
            folder_link = folder_heading
            try:
                folder_link = folder_heading.find_element(By.XPATH, ".//a")
            except NoSuchElementException:
                pass

            click_when_visible(driver, folder_link)
            isloaded(driver)
            collect_content(driver, collector, directory / folder_name, cancel_event=cancel_event, depth=depth + 1)
            if cancel_event is not None and cancel_event.is_set():
                return
            driver.back()
            isloaded(driver)

        elif item_type_lower == "item":
            try:
                item_name = validate_title(item.find_element(By.XPATH, './../*/h3').text)
            except NoSuchElementException:
                item_name = f"Item {index + 1}"

            item_dir = directory / item_name
            item_dir.mkdir(parents=True, exist_ok=True)
            log(f"{'  ' * depth}{item_name}/")

            for attachment in item.find_elements(By.XPATH, './../div[2]/div[1]/div[2]/ul/*/a[1]'):
                enqueue_task(
                    collector,
                    attachment.get_attribute("href"),
                    attachment.text,
                    item_dir,
                    "attachment",
                    page_url,
                )

            for table in item.find_elements(By.XPATH, './../div[2]/div/table'):
                collect_from_table(collector, table, item_dir, page_url)

        elif item_type_lower == "linked item":
            continue

        else:
            try:
                heading = item.find_element(By.XPATH, './../*/h3').text
            except NoSuchElementException:
                heading = f"item {index + 1}"
            log(f"{'  ' * depth}Needs review: {heading} ({item_type or 'unknown type'})")


def make_session(cookies):
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 Blackboard fully parallel downloader",
            "Referer": HOST,
        }
    )
    for cookie in cookies:
        kwargs = {"path": cookie.get("path", "/")}
        if cookie.get("domain"):
            kwargs["domain"] = cookie["domain"]
        session.cookies.set(cookie["name"], cookie["value"], **kwargs)
    return session


def filename_from_response(response, fallback):
    content_disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", content_disposition, flags=re.I)
    if match:
        return validate_title(unquote(match.group(1)))

    match = re.search(r'filename="?([^";]+)"?', content_disposition, flags=re.I)
    if match:
        return validate_title(match.group(1))

    path_name = Path(unquote(urlparse(response.url).path)).name
    return validate_title(path_name or fallback)


def known_file_suffix(name):
    return Path(name).suffix.lower() in KNOWN_FILE_EXTENSIONS


def extension_from_content_type(content_type):
    media_type = content_type.split(";", 1)[0].strip().lower()
    return CONTENT_TYPE_EXTENSIONS.get(media_type, "")


def final_name(label, server_name, content_type=""):
    if known_file_suffix(label):
        return validate_title(label)

    server_suffix = Path(server_name).suffix
    if server_suffix.lower() in KNOWN_FILE_EXTENSIONS:
        return validate_title(label + server_suffix)

    content_type_suffix = extension_from_content_type(content_type)
    if content_type_suffix:
        return validate_title(label + content_type_suffix)

    return validate_title(label)


def reserve_path(directory, filename, lock, reserved):
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / filename
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 1

    with lock:
        while candidate.exists() or str(candidate).lower() in reserved:
            candidate = directory / f"{stem} ({counter}){suffix}"
            counter += 1
        reserved.add(str(candidate).lower())
        return candidate


def download_one(task, cookies, lock, reserved):
    session = make_session(cookies)
    try:
        response = session.get(task.url, allow_redirects=True, stream=True, timeout=90)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        has_disposition = "content-disposition" in response.headers
        if "text/html" in content_type and not has_disposition:
            return DownloadResult(
                task=task,
                status="needs_browser",
                detail=f"URL returned HTML instead of a file: {response.url}",
            )

        server_name = filename_from_response(response, task.label)
        target = reserve_path(task.directory, final_name(task.label, server_name, content_type), lock, reserved)
        temp_path = target.with_name(f"{target.name}.{uuid.uuid4().hex}.part")

        with temp_path.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 256):
                if chunk:
                    output.write(chunk)
        temp_path.replace(target)
        return DownloadResult(task=task, status="downloaded", path=target)
    except Exception as exc:
        return DownloadResult(task=task, status="failed", detail=str(exc))
    finally:
        session.close()


def download_worker(worker_id, download_queue, cookies, path_lock, reserved, stats, results, dry_run):
    while True:
        task = download_queue.get()
        try:
            if task is STOP:
                return

            if dry_run:
                log(f"[dry-run worker {worker_id}] {task.directory / task.label} <- {task.url}")
                continue

            result = download_one(task, cookies, path_lock, reserved)
            results.append(result)
            with stats.lock:
                if result.status == "downloaded":
                    stats.downloaded += 1
                elif result.status == "needs_browser":
                    stats.needs_browser += 1
                else:
                    stats.failed += 1
                done = stats.downloaded + stats.needs_browser + stats.failed

            if result.status == "downloaded":
                log(f"[done {done}] saved {result.path}")
            else:
                log(f"[done {done}] {result.status}: {result.task.label} - {result.detail}")
        finally:
            download_queue.task_done()


def drain_download_queue(download_queue):
    drained = 0
    while True:
        try:
            download_queue.get_nowait()
            download_queue.task_done()
            drained += 1
        except queue.Empty:
            return drained


def scan_course(course, cookies, root_dir, collector, browser_config, headless, cancel_event=None):
    if cancel_event is not None and cancel_event.is_set():
        return
    course_name, course_url = course
    driver = prepare_scanner_driver(cookies, browser_config, headless=headless)
    try:
        if cancel_event is not None and cancel_event.is_set():
            return
        log(f"\n[scan] Course: {course_name}")
        course_dir = root_dir / course_name
        driver.get(course_url)
        isloaded(driver)
        if cancel_event is not None and cancel_event.is_set():
            return
        learning_material = find_visible_link_by_text(
            driver,
            ["Learning Materials", "Learning materials", "Course Content", "Content"],
        )
        if learning_material is None:
            log(f"[scan] No learning material link found for {course_name}")
            return
        click_when_visible(driver, learning_material)
        isloaded(driver)
        collect_content(driver, collector, course_dir, cancel_event=cancel_event)
    finally:
        driver.quit()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Scan Blackboard with multiple browser workers while download workers save files immediately."
    )
    parser.add_argument("--scan-workers", type=int, default=8, help="Number of parallel Selenium scanner browsers.")
    parser.add_argument("--download-workers", type=int, default=16, help="Number of parallel HTTP download workers.")
    parser.add_argument("--course", help="Only download courses whose name contains this text.")
    parser.add_argument("--dry-run", action="store_true", help="Scan and print tasks without downloading.")
    parser.add_argument("--show-scanners", action="store_true", help="Show scanner browser windows instead of headless scanners.")
    parser.add_argument("--no-ui", action="store_true", help="Skip login confirmation and review windows.")
    parser.add_argument("--output-folder", help="Folder where downloaded files are saved.")
    parser.add_argument(
        "--browser",
        choices=SUPPORTED_BROWSERS,
        default="auto",
        help="Browser to use. auto picks the most recently accessed supported browser executable.",
    )
    parser.add_argument("--types", help="Comma-separated extensions to download, for example pdf,docx,pptx.")
    parser.add_argument("--max-size-mb", type=float, help="Maximum file size to download automatically.")
    parser.add_argument("--exclude-unknown-types", action="store_true", help="Review files whose type cannot be detected.")
    parser.add_argument("--exclude-unknown-size", action="store_true", help="Review files whose size cannot be detected.")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def filter_options_from_args(args):
    allowed_extensions = parse_extensions(args.types) if args.types else frozenset(DEFAULT_ALLOWED_EXTENSIONS)
    max_size_bytes = None
    if args.max_size_mb is not None:
        max_size_bytes = max(0, int(args.max_size_mb * 1024 * 1024))
    return FilterOptions(
        allowed_extensions=allowed_extensions,
        max_size_bytes=max_size_bytes,
        keep_unknown_types=not args.exclude_unknown_types,
        keep_unknown_size=not args.exclude_unknown_size,
    )


def run_options_from_args(args, gui_mode=False):
    output_folder = Path(args.output_folder).expanduser() if args.output_folder else None
    return RunOptions(
        browser=args.browser,
        scan_workers=args.scan_workers,
        download_workers=args.download_workers,
        course=args.course,
        dry_run=args.dry_run,
        show_scanners=args.show_scanners,
        no_ui=args.no_ui,
        output_folder=output_folder,
        filters=filter_options_from_args(args),
        gui_mode=gui_mode,
    )


def get_run_root_dir(options):
    if options.output_folder:
        return options.output_folder.expanduser()
    return Path(get_root_dir()).expanduser()


def show_run_summary_popup(stats, needs_browser, failed, scan_errors, aborted=False):
    lines = [
        "Summary",
        f"Aborted by user: {'yes' if aborted else 'no'}",
        f"Found: {stats.found}",
        f"Filtered for review: {stats.filtered}",
        f"Downloaded: {stats.downloaded}",
        f"Needs browser/manual handling: {stats.needs_browser}",
        f"Failed: {stats.failed}",
    ]
    if scan_errors:
        lines.extend(["", "Scan errors:"])
        lines.extend(f"- {error}" for error in scan_errors[:5])
        if len(scan_errors) > 5:
            lines.append(f"- ...and {len(scan_errors) - 5} more")
    if failed:
        lines.extend(["", "Failed downloads:"])
        lines.extend(f"- {result.task.label}: {result.detail}" for result in failed[:5])
        if len(failed) > 5:
            lines.append(f"- ...and {len(failed) - 5} more")
    if needs_browser:
        lines.extend(["", f"{len(needs_browser)} link(s) need browser/manual handling."])
    if run_log_path:
        lines.extend(["", f"Run log: {run_log_path}"])
    show_gui_message("info", "Blackboard Saver summary", "\n".join(lines))


def run_with_options(options):
    runtime_paths = None
    login_driver = None

    try:
        try:
            init_run_log()
        except Exception as exc:
            report_error(options, "Run log error", f"Could not create the run log.\n\n{exc}")
            return 1

        try:
            runtime_paths = prepare_runtime_paths()
        except RuntimeError as exc:
            report_error(options, "Runtime folder error", str(exc))
            return 1

        try:
            browser_config = resolve_browser(options.browser)
        except RuntimeError as exc:
            report_error(options, "Browser error", str(exc))
            return 1

        try:
            root_dir = get_run_root_dir(options)
        except Exception as exc:
            report_error(options, "Download folder error", str(exc))
            return 1

        try:
            login_driver = make_driver(browser_config, headless=False)
            login(login_driver, runtime_paths.cookie_path, use_ui=not options.no_ui)
            courses = get_course_links(login_driver)
            if not courses and not options.no_ui:
                log("No courses were visible after loading cookies; asking for a fresh browser login.")
                if not wait_for_login_confirmation_ui():
                    raise RuntimeError("Login cancelled.")
                login_driver.get(HOST)
                isloaded(login_driver)
                save_cookie(login_driver, runtime_paths.cookie_path)
                courses = get_course_links(login_driver)
            if options.course:
                courses = [(name, href) for name, href in courses if options.course.lower() in name.lower()]
            cookies = login_driver.get_cookies()
        except RuntimeError as exc:
            report_error(options, "Login cancelled" if "cancelled" in str(exc).lower() else "Login error", str(exc))
            return 1
        except Exception as exc:
            report_error(options, "Browser error", f"Could not start or use the selected browser.\n\n{exc}")
            return 1
        finally:
            if login_driver is not None:
                try:
                    login_driver.quit()
                except Exception:
                    pass

        if not courses:
            message = "No matching courses found."
            log(message)
            if options.gui_mode:
                show_gui_message("info", "Blackboard Saver", message)
            return 0

        cancel_event = threading.Event()
        progress = ProgressWindow(cancel_event=cancel_event) if options.gui_mode else None

        stats = Stats()
        download_queue = queue.Queue(maxsize=max(16, options.download_workers * 4))
        results = []
        path_lock = threading.Lock()
        reserved = set()

        download_threads = []
        for worker_id in range(1, max(1, options.download_workers) + 1):
            thread = threading.Thread(
                target=download_worker,
                args=(worker_id, download_queue, cookies, path_lock, reserved, stats, results, options.dry_run),
                daemon=True,
            )
            thread.start()
            download_threads.append(thread)

        collector = TaskCollector(cookies, options.filters, stats, download_queue)

        scan_workers = max(1, options.scan_workers)
        log(
            f"Scanning {len(courses)} courses with {scan_workers} scanner(s); "
            f"matching files will download immediately."
        )
        if progress:
            progress.set_status("Scanning courses and downloading matching files...")

        scan_errors = []
        with ThreadPoolExecutor(max_workers=scan_workers) as executor:
            pending = {
                executor.submit(
                    scan_course,
                    course,
                    cookies,
                    root_dir,
                    collector,
                    browser_config,
                    not options.show_scanners,
                    cancel_event,
                )
                for course in courses
            }
            while pending:
                if cancel_event.is_set():
                    log("Cancelling queued scan work...")
                    for future in pending:
                        future.cancel()
                    pending = {future for future in pending if not future.cancelled()}
                    if not pending:
                        break
                done, pending = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
                if progress:
                    progress.pump()
                for future in done:
                    if future.cancelled():
                        continue
                    try:
                        future.result()
                    except Exception as exc:
                        message = f"[scan failed] {exc}"
                        scan_errors.append(str(exc))
                        log(message)

        if progress:
            if cancel_event.is_set():
                progress.set_status("Abort requested. Stopping downloads...")
            else:
                progress.set_status("Scan finished. Waiting for downloads to finish...")

        if collector.rejected and not options.no_ui and not cancel_event.is_set() and progress:
            progress.close()
            progress = None

        selected_rejected = []
        if collector.rejected and not options.no_ui and not cancel_event.is_set():
            selected_rejected = review_filtered_candidates_ui(collector.rejected)
            if options.gui_mode:
                progress = ProgressWindow(cancel_event=cancel_event)
                progress.set_status("Downloading selected files...")

        if not cancel_event.is_set():
            for candidate in selected_rejected:
                download_queue.put(candidate.task)
        else:
            drained = drain_download_queue(download_queue)
            if drained:
                log(f"Discarded {drained} queued download(s).")

        log(
            f"\nAccepted immediately: {len(collector.accepted)} file(s). "
            f"Manually kept: {len(selected_rejected)}. "
            f"Filtered out: {len(collector.rejected) - len(selected_rejected)}"
        )
        if cancel_event.is_set():
            log("Run aborted by user. Active browser/download work may finish before shutdown.")

        while download_queue.unfinished_tasks:
            if cancel_event.is_set():
                drained = drain_download_queue(download_queue)
                if drained:
                    log(f"Discarded {drained} queued download(s).")
            if progress:
                progress.pump()
            time.sleep(0.25)

        for _ in download_threads:
            download_queue.put(STOP)
        for thread in download_threads:
            while thread.is_alive():
                if progress:
                    progress.pump()
                thread.join(timeout=0.25)

        if progress:
            progress.set_status("Aborted." if cancel_event.is_set() else "Finished.")
            progress.close()
            progress = None

        log("\nSummary")
        log(f"Found: {stats.found}")
        log(f"Filtered for review: {stats.filtered}")
        log(f"Downloaded: {stats.downloaded}")
        log(f"Needs browser/manual handling: {stats.needs_browser}")
        log(f"Failed: {stats.failed}")

        needs_browser = [result for result in results if result.status == "needs_browser"]
        failed = [result for result in results if result.status == "failed"]

        if needs_browser:
            log("\nThese links probably require an extra Blackboard click or a new scraper rule:")
            for result in needs_browser:
                log(f"- {result.task.label}: {result.task.url}")

        if failed:
            log("\nFailed downloads:")
            for result in failed:
                log(f"- {result.task.label}: {result.detail}")

        if cancel_event.is_set():
            log("Aborted: yes")

        if options.gui_mode:
            show_run_summary_popup(stats, needs_browser, failed, scan_errors, aborted=cancel_event.is_set())

        return 0
    except Exception as exc:
        report_error(options, "Blackboard Saver error", f"Unexpected error.\n\n{exc}")
        return 1
    finally:
        if "progress" in locals() and progress:
            progress.close()
        if runtime_paths is not None:
            cleanup_runtime_dir(runtime_paths.runtime_dir)
        close_run_log()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(argv)
    if args.smoke_test:
        return 0

    if not argv:
        options = show_launch_options_ui(run_options_from_args(args, gui_mode=True))
        if options is None:
            log("Cancelled.")
            return 0
        return run_with_options(options)

    return run_with_options(run_options_from_args(args, gui_mode=False))


if __name__ == "__main__":
    raise SystemExit(main())
