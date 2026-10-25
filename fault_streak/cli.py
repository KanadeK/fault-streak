import argparse
import json
import sys

from . import __version__
from .core import InputError, audit_files


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare ordered faulthandler files for unchanged Python stack streaks."
    )
    parser.add_argument(
        "files", nargs="+", help="one all-threads snapshot per file, in capture order"
    )
    parser.add_argument("--min-streak", type=positive, default=2)
    parser.add_argument("--samples", type=positive, default=20)
    parser.add_argument("--include-frames", action="store_true")
    parser.add_argument(
        "--fail-on-streak",
        action="store_true",
        help="exit 1 when any qualifying streak is observed",
    )
    parser.add_argument("--max-bytes", type=positive, default=8388608)
    parser.add_argument("--max-total-bytes", type=positive, default=67108864)
    parser.add_argument("--max-snapshots", type=positive, default=128)
    parser.add_argument("--max-threads", type=positive, default=1024)
    parser.add_argument("--max-frames", type=positive, default=1000)
    parser.add_argument(
        "--frame-cap-hint",
        type=positive,
        default=100,
        help="exclude stacks this deep because CPython may silently truncate (default 100)",
    )
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)
    try:
        options = vars(args).copy()
        files = options.pop("files")
        fail_on_streak = options.pop("fail_on_streak")
        report = audit_files(files, **options)
        print(json.dumps(report, ensure_ascii=True, indent=2))
        return int(report["warning_count"] > 0 or (fail_on_streak and report["streak_count"] > 0))
    except InputError as exc:
        print(json.dumps({"complete": False, "error": str(exc)}), file=sys.stderr)
        return 2
    except OSError:
        print('{"complete": false, "error": "input or output unavailable"}', file=sys.stderr)
        return 2
