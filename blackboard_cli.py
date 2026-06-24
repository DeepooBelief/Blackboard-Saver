import argparse
import copy
import json
import os
import sys
import webbrowser
from pathlib import Path

import blackboard_fully_parallel as saver
from config import DEFAULT_ROOT_DIR


PREFERENCE_FILE = "pref.cli.json"


def _extension_name(extension):
    return str(extension).strip().lower().lstrip(".")


def _default_preferences():
    return {
        "browser": "auto",
        "scan_workers": 8,
        "download_workers": 16,
        "course": "",
        "output_folder": str(DEFAULT_ROOT_DIR),
        "dry_run": False,
        "show_scanners": False,
        "review_filtered": True,
        "filters": {
            "types": sorted(_extension_name(ext) for ext in saver.DEFAULT_ALLOWED_EXTENSIONS),
            "max_size_mb": None,
            "keep_unknown_types": True,
            "keep_unknown_size": True,
        },
    }


def preference_path(value=None):
    if value:
        return Path(os.path.expandvars(value)).expanduser()
    return Path(__file__).resolve().with_name(PREFERENCE_FILE)


def normalize_preferences(raw):
    prefs = _default_preferences()
    raw = raw or {}
    for key in (
        "browser",
        "scan_workers",
        "download_workers",
        "course",
        "output_folder",
        "dry_run",
        "show_scanners",
        "review_filtered",
    ):
        if key in raw:
            prefs[key] = raw[key]

    filters = raw.get("filters") or {}
    for key in ("types", "max_size_mb", "keep_unknown_types", "keep_unknown_size"):
        if key in filters:
            prefs["filters"][key] = filters[key]

    prefs["browser"] = str(prefs["browser"] or "auto").lower()
    prefs["scan_workers"] = max(1, int(prefs["scan_workers"]))
    prefs["download_workers"] = max(1, int(prefs["download_workers"]))
    prefs["course"] = str(prefs["course"] or "").strip()
    prefs["output_folder"] = str(prefs["output_folder"] or DEFAULT_ROOT_DIR)
    prefs["dry_run"] = bool(prefs["dry_run"])
    prefs["show_scanners"] = bool(prefs["show_scanners"])
    prefs["review_filtered"] = bool(prefs["review_filtered"])

    types = prefs["filters"].get("types")
    if isinstance(types, str):
        types = [part for part in types.replace(";", ",").split(",")]
    prefs["filters"]["types"] = sorted(
        {name for name in (_extension_name(ext) for ext in (types or [])) if name}
    )

    max_size = prefs["filters"].get("max_size_mb")
    if max_size in ("", "none", "null", "unlimited"):
        max_size = None
    elif max_size is not None:
        max_size = float(max_size)
        if max_size <= 0:
            raise ValueError("filters.max_size_mb must be positive or null.")
    prefs["filters"]["max_size_mb"] = max_size
    prefs["filters"]["keep_unknown_types"] = bool(prefs["filters"]["keep_unknown_types"])
    prefs["filters"]["keep_unknown_size"] = bool(prefs["filters"]["keep_unknown_size"])

    if prefs["browser"] not in saver.SUPPORTED_BROWSERS:
        raise ValueError(f"browser must be one of: {', '.join(saver.SUPPORTED_BROWSERS)}")

    return prefs


def read_preferences(path):
    if not path.exists():
        prefs = _default_preferences()
        write_preferences(path, prefs)
        return prefs, True

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Could not parse {path}: {exc}") from exc
    return normalize_preferences(raw), False


def write_preferences(path, prefs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalize_preferences(prefs), indent=2) + "\n", encoding="utf-8")


def print_browser_candidates():
    candidates = saver.find_browser_candidates("auto")
    if not candidates:
        print("Detected browsers: none found")
        return
    print("Detected browsers:")
    for candidate in candidates[:8]:
        location = f" ({candidate.executable})" if candidate.executable else ""
        print(f"  - {candidate.label}{location}")


def print_preferences(prefs, path):
    filters = prefs["filters"]
    max_size = filters["max_size_mb"]
    print(f"Preferences: {path}")
    print(f"  browser: {prefs['browser']}")
    print(f"  course: {prefs['course'] or 'all courses'}")
    print(f"  output_folder: {prefs['output_folder']}")
    print(f"  scan_workers: {prefs['scan_workers']}")
    print(f"  download_workers: {prefs['download_workers']}")
    print(f"  dry_run: {prefs['dry_run']}")
    print(f"  show_scanners: {prefs['show_scanners']}")
    print(f"  review_filtered: {prefs['review_filtered']}")
    print(f"  types: {', '.join(filters['types']) or 'none'}")
    print(f"  max_size_mb: {max_size if max_size is not None else 'unlimited'}")
    print(f"  keep_unknown_types: {filters['keep_unknown_types']}")
    print(f"  keep_unknown_size: {filters['keep_unknown_size']}")


def prompt_text(label, default="", allow_empty=True):
    suffix = f" [{default}]" if default not in ("", None) else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if value:
            return value
        if allow_empty:
            return "" if default is None else default
        print("Please enter a value.")


def prompt_bool(label, default=False):
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{suffix}]: ").strip().lower()
        if not value:
            return bool(default)
        if value in ("y", "yes", "true", "1", "on"):
            return True
        if value in ("n", "no", "false", "0", "off"):
            return False
        print("Enter yes or no.")


def prompt_int(label, default):
    while True:
        value = prompt_text(label, str(default))
        try:
            parsed = int(value)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
        print("Enter a positive integer.")


def prompt_optional_float(label, default):
    default_text = "" if default is None else str(default)
    while True:
        value = prompt_text(label, default_text).strip().lower()
        if value in ("", "none", "null", "unlimited"):
            return None
        try:
            parsed = float(value)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
        print("Enter a positive number, or leave blank for unlimited.")


def prompt_browser(default):
    choices = ", ".join(saver.SUPPORTED_BROWSERS)
    while True:
        value = prompt_text(f"Browser ({choices})", default).lower()
        if value in saver.SUPPORTED_BROWSERS:
            return value
        print(f"Choose one of: {choices}")


def prompt_types(current):
    known = sorted(_extension_name(ext) for ext in saver.KNOWN_FILE_EXTENSIONS)
    default = sorted(_extension_name(ext) for ext in saver.DEFAULT_ALLOWED_EXTENSIONS)
    print("File types may be a comma list, or one of: default, all, none.")
    print(f"Known types: {', '.join(known)}")
    while True:
        value = prompt_text("File types", ",".join(current)).strip().lower()
        if value == "default":
            return default
        if value == "all":
            return known
        if value == "none":
            return []
        names = sorted({name for name in (_extension_name(part) for part in value.split(",")) if name})
        if names:
            return names
        if prompt_bool("No file types selected. Keep none", False):
            return []


def edit_preferences(prefs):
    prefs = copy.deepcopy(prefs)
    filters = prefs["filters"]
    print("\nEdit preferences. Press Enter to keep the current value.\n")
    print_browser_candidates()
    prefs["browser"] = prompt_browser(prefs["browser"])
    prefs["course"] = prompt_text("Course contains (blank for all)", prefs["course"])
    prefs["output_folder"] = prompt_text("Download folder", prefs["output_folder"], allow_empty=False)
    prefs["scan_workers"] = prompt_int("Scan workers", prefs["scan_workers"])
    prefs["download_workers"] = prompt_int("Download workers", prefs["download_workers"])
    prefs["dry_run"] = prompt_bool("Dry run", prefs["dry_run"])
    prefs["show_scanners"] = prompt_bool("Show scanner browser windows", prefs["show_scanners"])
    prefs["review_filtered"] = prompt_bool("Review filtered files after scanning", prefs["review_filtered"])
    filters["types"] = prompt_types(filters["types"])
    filters["max_size_mb"] = prompt_optional_float("Maximum file size in MB (blank for unlimited)", filters["max_size_mb"])
    filters["keep_unknown_types"] = prompt_bool("Keep files with unknown type", filters["keep_unknown_types"])
    filters["keep_unknown_size"] = prompt_bool("Keep files with unknown size", filters["keep_unknown_size"])
    return normalize_preferences(prefs)


def parse_max_size(value):
    if value is None:
        return None
    value = str(value).strip().lower()
    if value in ("", "none", "null", "unlimited"):
        return None
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("--max-size-mb must be positive, or use 'none'.")
    return parsed


def apply_cli_overrides(prefs, args):
    prefs = copy.deepcopy(prefs)
    filters = prefs["filters"]
    for key in ("browser", "scan_workers", "download_workers", "course", "output_folder"):
        value = getattr(args, key, None)
        if value is not None:
            prefs[key] = value
    for key in ("dry_run", "show_scanners", "review_filtered"):
        value = getattr(args, key, None)
        if value is not None:
            prefs[key] = value
    if args.types is not None:
        filters["types"] = sorted(_extension_name(ext) for ext in saver.parse_extensions(args.types))
    if args.max_size_mb is not None:
        filters["max_size_mb"] = parse_max_size(args.max_size_mb)
    if args.keep_unknown_types is not None:
        filters["keep_unknown_types"] = args.keep_unknown_types
    if args.keep_unknown_size is not None:
        filters["keep_unknown_size"] = args.keep_unknown_size
    return normalize_preferences(prefs)


def preferences_to_options(prefs):
    filters = prefs["filters"]
    max_size_mb = filters["max_size_mb"]
    max_size_bytes = None if max_size_mb is None else int(max_size_mb * 1024 * 1024)
    output_folder = Path(os.path.expandvars(prefs["output_folder"])).expanduser()
    return saver.RunOptions(
        browser=prefs["browser"],
        scan_workers=prefs["scan_workers"],
        download_workers=prefs["download_workers"],
        course=prefs["course"] or None,
        dry_run=prefs["dry_run"],
        show_scanners=prefs["show_scanners"],
        no_ui=False,
        output_folder=output_folder,
        filters=saver.FilterOptions(
            allowed_extensions=saver.parse_extensions(",".join(filters["types"])),
            max_size_bytes=max_size_bytes,
            keep_unknown_types=filters["keep_unknown_types"],
            keep_unknown_size=filters["keep_unknown_size"],
        ),
        gui_mode=False,
    )


def wait_for_login_confirmation_cli():
    print("\nFinish logging in to Blackboard in the browser.")
    value = input("When the Blackboard course page is visible, press Enter to scan, or type cancel: ").strip().lower()
    return value not in ("cancel", "c", "quit", "q", "exit")


def _candidate_line(index, candidate):
    path = candidate.task.directory / candidate.filename
    reasons = "; ".join(candidate.reasons) or "filtered"
    return f"{index}. {path} ({reasons})"


def parse_selection(value, count):
    selected = set()
    for part in value.replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                start, end = end, start
            selected.update(range(start, end + 1))
        else:
            selected.add(int(part))
    invalid = [index for index in selected if index < 1 or index > count]
    if invalid:
        raise ValueError(f"Selection out of range: {invalid[0]}")
    return {index - 1 for index in selected}


def review_filtered_candidates_cli(candidates):
    if not candidates:
        return []

    print(f"\n{len(candidates)} filtered file(s) are available for review.")
    print("Commands: none, all, list, select 1,3-5, open 2, help")
    while True:
        command = input("Review command [none]: ").strip()
        if not command:
            command = "none"
        lower = command.lower()

        if lower in ("none", "n", "skip"):
            return []
        if lower in ("all", "a"):
            return list(candidates)
        if lower in ("help", "?", "h"):
            print("none: skip filtered files")
            print("all: download every filtered file")
            print("list: show filtered files; use 'list all' to show every item")
            print("select 1,3-5: download selected item numbers")
            print("open 2: open the original Blackboard page for item 2")
            continue
        if lower.startswith("list"):
            parts = lower.split()
            limit = len(candidates) if len(parts) > 1 and parts[1] == "all" else min(len(candidates), 50)
            for index, candidate in enumerate(candidates[:limit], start=1):
                print(_candidate_line(index, candidate))
                if candidate.task.page_url:
                    print(f"   page: {candidate.task.page_url}")
            if limit < len(candidates):
                print(f"Showing first {limit}; use 'list all' for the full list.")
            continue
        if lower.startswith("open"):
            value = command[4:].strip()
            try:
                indexes = parse_selection(value, len(candidates))
            except (TypeError, ValueError):
                print("Use an item number, for example: open 2")
                continue
            for index in sorted(indexes):
                url = candidates[index].task.page_url or candidates[index].task.url
                webbrowser.open(url)
            continue
        if lower.startswith("select") or lower.startswith("keep"):
            value = command.split(None, 1)[1] if len(command.split(None, 1)) == 2 else ""
            try:
                indexes = parse_selection(value, len(candidates))
            except (TypeError, ValueError) as exc:
                print(f"Invalid selection: {exc}")
                continue
            return [candidate for index, candidate in enumerate(candidates) if index in indexes]
        print("Unknown command. Type help for review commands.")


def accept_liability(accepted):
    if accepted:
        return True
    print("\n" + saver.LIABILITY_TEXT)
    value = input("Type yes to accept and start login: ").strip().lower()
    return value in ("yes", "y")


def build_parser():
    parser = argparse.ArgumentParser(
        description="CLI launcher for Blackboard Saver. Preferences are read from pref.cli.json by default."
    )
    parser.add_argument("--preferences", help="Path to the CLI preference file.")
    parser.add_argument("--write-default-preferences", action="store_true", help="Write default preferences and exit.")
    parser.add_argument("--edit-preferences", action="store_true", help="Edit preferences interactively before running.")
    parser.add_argument("--print-preferences", action="store_true", help="Print resolved preferences and exit.")
    parser.add_argument("--list-browsers", action="store_true", help="Print detected browsers and exit.")
    parser.add_argument("--accept-liability", action="store_true", help="Accept the liability statement for this run.")
    parser.add_argument("--browser", choices=saver.SUPPORTED_BROWSERS)
    parser.add_argument("--scan-workers", type=int)
    parser.add_argument("--download-workers", type=int)
    parser.add_argument("--course")
    parser.add_argument("--output-folder")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--show-scanners", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--review-filtered", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--types", help="Comma-separated extensions to download, or an empty string for none.")
    parser.add_argument("--max-size-mb", help="Maximum file size in MB, or 'none' for unlimited.")
    parser.add_argument("--keep-unknown-types", dest="keep_unknown_types", action="store_true", default=None)
    parser.add_argument("--exclude-unknown-types", dest="keep_unknown_types", action="store_false")
    parser.add_argument("--keep-unknown-size", dest="keep_unknown_size", action="store_true", default=None)
    parser.add_argument("--exclude-unknown-size", dest="keep_unknown_size", action="store_false")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.smoke_test:
        return 0

    path = preference_path(args.preferences)
    if args.list_browsers:
        print_browser_candidates()
        return 0
    if args.write_default_preferences:
        write_preferences(path, _default_preferences())
        print(f"Wrote {path}")
        return 0

    try:
        prefs, created = read_preferences(path)
        if created:
            print(f"Created default preferences at {path}")
        if args.edit_preferences:
            prefs = edit_preferences(prefs)
            write_preferences(path, prefs)
            print(f"Saved {path}")

        prefs = apply_cli_overrides(prefs, args)
        if args.print_preferences:
            print_preferences(prefs, path)
            return 0

        print_preferences(prefs, path)
        if sys.stdin.isatty() and not args.edit_preferences:
            if prompt_bool("Edit preferences before this run", False):
                prefs = edit_preferences(prefs)
                write_preferences(path, prefs)
                print(f"Saved {path}")

        if not accept_liability(args.accept_liability):
            print("Cancelled.")
            return 1

        saver.wait_for_login_confirmation_ui = wait_for_login_confirmation_cli
        saver.review_filtered_candidates_ui = (
            review_filtered_candidates_cli if prefs["review_filtered"] else lambda _candidates: []
        )

        try:
            return saver.run_with_options(preferences_to_options(prefs))
        except KeyboardInterrupt:
            print("\nInterrupted by user.")
            return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
