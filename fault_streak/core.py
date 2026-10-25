"""Strict parsing and consecutive observation comparison. No process attachment."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

HEADER = re.compile(r"(Current thread|Thread) (0x[0-9a-fA-F]{1,32}) \(most recent call first\):")
FRAME = re.compile(r'  File (".*"|\?\?\?), line ([0-9]{1,10}|\?\?\?) in (.+)')
TIMEOUT = re.compile(r"Timeout \([0-9]+:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?\)!")
MARKERS = {"  <no Python frame>", "  <freed frame>", "  <invalid frame>", "  <freed thread state>"}


class InputError(ValueError):
    """Unsupported or malformed snapshot, never silently skipped."""


@dataclass
class ThreadStack:
    tid: str
    header_line: int
    frames: list[tuple[str, int | None, str]] = field(default_factory=list)
    frame_lines: list[int] = field(default_factory=list)
    incomplete: bool = False

    @property
    def comparable(self):
        return bool(self.frames) and not self.incomplete

    @property
    def signature(self):
        return tuple(self.frames)


@dataclass
class Snapshot:
    threads: dict[str, ThreadStack]
    warnings: list[dict]
    incomplete: bool
    sha256: str
    byte_count: int


def parse_snapshot(
    raw, *, max_bytes=8388608, max_threads=1024, max_frames=1000, frame_cap_hint=100
):
    """Parse one all-threads dump, preserving displayed frame strings literally.

    CPython can silently cap frame depth. At frame_cap_hint or more frames a stack
    is conservatively excluded. A raw global ellipsis marks unknown thread coverage.
    No decoding of backslash escapes, source-file opening, or code execution occurs.
    """
    if not isinstance(raw, bytes):
        raise InputError("snapshot must be bytes")
    if any(
        type(v) is not int or v < 1 for v in (max_bytes, max_threads, max_frames, frame_cap_hint)
    ):
        raise InputError("limits must be positive integers")
    if len(raw) > max_bytes:
        raise InputError("snapshot byte budget exceeded")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise InputError("snapshot is not UTF-8") from exc
    threads = {}
    warnings = []
    current = None
    timeout_seen = False
    current_headers = 0
    global_end = False
    incomplete = False

    def warn(code, line, stack=None):
        warnings.append({"code": code, "line": line, "thread_id": stack.tid if stack else None})

    for number, line in enumerate(text.replace("\r\n", "\n").split("\n"), 1):
        if line == "":
            continue
        if global_end:
            raise InputError(f"line {number}: content after thread truncation marker")
        match = HEADER.fullmatch(line)
        if match:
            if match.group(1) == "Current thread":
                current_headers += 1
                if current_headers > 1:
                    raise InputError(
                        f"line {number}: multiple current threads; split concatenated dumps"
                    )
            if current is not None and not current.frames and not current.incomplete:
                raise InputError(f"line {current.header_line}: thread header without stack")
            tid = hex(int(match.group(2), 16))
            if tid in threads:
                raise InputError(
                    f"line {number}: repeated thread ID; one snapshot per file is required"
                )
            if len(threads) >= max_threads:
                raise InputError("thread budget exceeded")
            current = ThreadStack(tid, number)
            threads[tid] = current
            continue
        if TIMEOUT.fullmatch(line):
            if threads or timeout_seen:
                raise InputError(f"line {number}: multiple or misplaced timeout headers")
            timeout_seen = True
            continue
        if line == "..." and threads:
            incomplete = True
            global_end = True
            warn("thread_list_truncated", number)
            continue
        if current is None:
            raise InputError(f"line {number}: expected all-threads faulthandler header")
        match = FRAME.fullmatch(line)
        if match:
            if current.incomplete:
                raise InputError(f"line {number}: frame after incomplete-stack marker")
            if len(current.frames) >= max_frames:
                raise InputError("frame budget exceeded")
            filename, lineno, function = match.groups()
            filename = filename[1:-1] if filename.startswith('"') else filename
            frame = (filename, None if lineno == "???" else int(lineno), function)
            current.frames.append(frame)
            current.frame_lines.append(number)
            continue
        if line in MARKERS or line == "  ..." or re.fullmatch(r"plus [0-9]+ frames", line):
            current.incomplete = True
            incomplete = True
            warn("stack_incomplete_or_unavailable", number, current)
            continue
        if line == "  Garbage-collecting":
            warn("garbage_collection_observed", number, current)
            continue
        raise InputError(
            f"line {number}: unsupported content; use a single plain Python-thread dump"
        )
    if not threads:
        raise InputError("no thread stacks found")
    if current is not None and not current.frames and not current.incomplete:
        raise InputError(f"line {current.header_line}: thread header without stack")
    if raw and not raw.endswith(b"\n"):
        incomplete = True
        current.incomplete = True
        warn("missing_final_newline", len(text.split("\n")), current)
    for stack in threads.values():
        if len(stack.frames) >= frame_cap_hint:
            stack.incomplete = True
            incomplete = True
            warn("possible_frame_cap", stack.header_line, stack)
        if any(
            f == "???" or n is None or fn == "???" or f.endswith("...") or fn.endswith("...")
            for f, n, fn in stack.frames
        ):
            stack.incomplete = True
            incomplete = True
            warn("unknown_or_possibly_clipped_frame_text", stack.header_line, stack)
    return Snapshot(threads, warnings, incomplete, hashlib.sha256(raw).hexdigest(), len(raw))


def compare(snapshots, *, min_streak=2, samples=20, include_frames=False):
    """Find maximal runs with the same thread ID and complete displayed stack.

    Any missing/changed/incomplete observation breaks a run. No assertion about
    elapsed time, process progress, lock ownership or thread identity reuse follows.
    """
    if len(snapshots) < 2:
        raise InputError("at least two ordered snapshots are required")
    if type(min_streak) is not int or min_streak < 2 or type(samples) is not int or samples < 1:
        raise InputError("min-streak must be at least 2 and samples must be positive")
    active = {}
    streaks = []
    streak_count = 0
    transitions = []
    transition_counts = Counter()
    warning_count = 0
    warning_examples = []

    def finish(tid):
        nonlocal streak_count
        run = active.pop(tid)
        if run["observations"] < min_streak:
            return
        streak_count += 1
        if len(streaks) < samples:
            stack = run.pop("stack")
            signature = json.dumps(
                stack.signature, ensure_ascii=True, separators=(",", ":")
            ).encode()
            run["thread_id"] = tid
            run["stack_sha256"] = hashlib.sha256(signature).hexdigest()
            run["frame_count"] = len(stack.frames)
            run["top_frame"] = dict(zip(("file", "line", "function"), stack.frames[0], strict=True))
            if include_frames:
                run["frames"] = [
                    dict(zip(("file", "line", "function"), frame, strict=True))
                    for frame in stack.frames
                ]
            streaks.append(run)

    previous = None
    for index, snapshot in enumerate(snapshots, 1):
        for warning in snapshot.warnings:
            warning_count += 1
            if len(warning_examples) < samples:
                warning_examples.append({"snapshot": index, **warning})
        if previous is not None:
            for tid in sorted(
                set(previous.threads) | set(snapshot.threads), key=lambda v: int(v, 16)
            ):
                before, after = previous.threads.get(tid), snapshot.threads.get(tid)
                if before is None:
                    status = "newly_observed"
                elif after is None:
                    status = "no_longer_observed"
                elif not before.comparable or not after.comparable:
                    status = "incomparable"
                elif before.signature == after.signature:
                    status = "unchanged"
                else:
                    status = "changed"
                transition_counts[status] += 1
                if status != "unchanged" and len(transitions) < samples:
                    transitions.append(
                        {
                            "from_snapshot": index - 1,
                            "to_snapshot": index,
                            "thread_id": tid,
                            "status": status,
                            "coverage_uncertain": previous.incomplete or snapshot.incomplete,
                            "before_line": before.header_line if before else None,
                            "after_line": after.header_line if after else None,
                        }
                    )
        for tid in list(active):
            stack = snapshot.threads.get(tid)
            if (
                stack is None
                or not stack.comparable
                or active[tid]["stack"].signature != stack.signature
            ):
                finish(tid)
        for tid in sorted(snapshot.threads, key=lambda v: int(v, 16)):
            stack = snapshot.threads[tid]
            if not stack.comparable:
                continue
            if tid in active:
                active[tid]["last_snapshot"] = index
                active[tid]["last_header_line"] = stack.header_line
                active[tid]["observations"] += 1
            else:
                active[tid] = {
                    "first_snapshot": index,
                    "last_snapshot": index,
                    "first_header_line": stack.header_line,
                    "last_header_line": stack.header_line,
                    "observations": 1,
                    "stack": stack,
                }
        previous = snapshot
    for tid in list(active):
        finish(tid)
    return {
        "schema_version": 1,
        "complete": True,
        "min_streak": min_streak,
        "snapshot_count": len(snapshots),
        "streak_count": streak_count,
        "streak_examples": streaks,
        "streak_examples_omitted": max(0, streak_count - samples),
        "transition_counts": dict(sorted(transition_counts.items())),
        "transition_examples": transitions,
        "warning_count": warning_count,
        "warning_examples": warning_examples,
        "snapshots": [
            {
                "snapshot": i,
                "sha256": s.sha256,
                "bytes": s.byte_count,
                "threads_observed": len(s.threads),
                "coverage_uncertain": s.incomplete,
                "comparable_stacks": sum(t.comparable for t in s.threads.values()),
            }
            for i, s in enumerate(snapshots, 1)
        ],
        "example_limit": samples,
    }


def audit_files(
    paths,
    *,
    max_bytes=8388608,
    max_total_bytes=67108864,
    max_snapshots=128,
    max_threads=1024,
    max_frames=1000,
    frame_cap_hint=100,
    min_streak=2,
    samples=20,
    include_frames=False,
):
    limits = (max_bytes, max_total_bytes, max_snapshots, max_threads, max_frames, frame_cap_hint)
    if any(type(v) is not int or v < 1 for v in limits):
        raise InputError("limits must be positive integers")
    if not 2 <= len(paths) <= max_snapshots:
        raise InputError("provide 2..max-snapshots files, in capture order")
    snapshots = []
    total = 0
    for index, path in enumerate(paths, 1):
        with Path(path).open("rb") as handle:
            raw = handle.read(min(max_bytes, max_total_bytes - total) + 1)
        total += len(raw)
        if total > max_total_bytes:
            raise InputError("total capture byte budget exceeded")
        try:
            snapshots.append(
                parse_snapshot(
                    raw,
                    max_bytes=max_bytes,
                    max_threads=max_threads,
                    max_frames=max_frames,
                    frame_cap_hint=frame_cap_hint,
                )
            )
        except InputError as exc:
            raise InputError(f"snapshot {index}: {exc}") from exc
    return compare(snapshots, min_streak=min_streak, samples=samples, include_frames=include_frames)
