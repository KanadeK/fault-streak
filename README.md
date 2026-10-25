# fault-streak

Find consecutive unchanged Python thread stacks in ordered CPython `faulthandler` captures. Compare thread identities and full displayed stacks, ignoring thread-list order. Get exact snapshot/header-line witnesses, changed/new/missing observations, and warnings when truncation makes the evidence incomplete.

Useful when a hung Python process has already produced several dumps and live attachment is unavailable. It reads local files; it never attaches to processes, sends signals, opens referenced source files or executes captured text.

**An unchanged stack is an observation, not proof of a deadlock.** A healthy idle worker, a repeating loop, or activity between samples can look unchanged. Thread IDs can be reused. Compare captures from one process lifetime and combine the results with application knowledge.

## Install and use

Python 3.10+; no runtime dependencies. GitHub source/wheel distribution, not a PyPI publication.

```sh
git clone https://github.com/KanadeK/fault-streak.git
cd fault-streak
python -m venv .venv
# Linux/macOS: . .venv/bin/activate
# PowerShell: .venv/Scripts/Activate.ps1
python -m pip install .
fault-streak examples/first.txt examples/second.txt
fault-streak examples/first.txt examples/second.txt --fail-on-streak --include-frames
```

The synthetic examples have one unchanged worker stack while the main thread changes line and the thread order changes. The first command reports one streak and exits **0**. The second deliberately exits **1** because `--fail-on-streak` is enabled.

Generate actual captures of a disposable demo process:

```sh
python examples/capture_demo.py demo-captures
fault-streak demo-captures/first.txt demo-captures/second.txt --include-frames
```

The demo refuses an existing output directory and releases its waiting thread before exit. The captures contain your machine's source paths. Delete only that demo output directory when no longer needed. `python -m fault_streak` is equivalent to the CLI. Uninstall with `python -m pip uninstall fault-streak`; remove the dedicated virtual environment if desired.

## Supported capture contract

Pass one **all-threads Python dump per file**, in capture order. Capture order comes from arguments, not file names or modification times. Use `faulthandler.dump_traceback(file=..., all_threads=True)` with a separate file for each observation; keep the file open for the duration of the dump. Plain CPython 3.10–3.13 headers/frames are the tested contract. A leading `Timeout (h:mm:ss)!` is accepted, as are UTF-8 BOM and CRLF.

The tool recognizes `Thread 0x...` / `Current thread 0x...` headers, frame lines, thread/stack truncation markers, unavailable frames and the garbage-collection notice. It preserves displayed backslash escapes literally. It rejects mixed application log lines, ordinary exception tracebacks, single-stack dumps without thread IDs, C-stack sections, malformed headers and detectable concatenated dumps. Strip or split such inputs deliberately before analysis. Not every concatenation is detectable if thread sets are disjoint and no repeated current-thread/timeout header exists.

The input files must be stable while read. The tool cannot know whether a syntactically complete capture silently omitted entire threads. Future CPython formats may require a parser update; Python 3.14+ native C-stack sections are not supported.

## Meaning of a streak

The same normalized numeric thread ID must have the exact same ordered `(displayed filename, line number, function name)` stack in adjacent snapshots. Current-thread labeling and header position do not affect equality. Any missing, changed or incomplete observation breaks a run. Runs are maximal, without overlapping subruns; `--min-streak 3` requires three consecutive observations. Reappearance after an absent sample starts a new run.

CPython can cap frame depth without an obvious ellipsis. By default **stacks with 100 or more frames are excluded** and marked `possible_frame_cap`, even when they might be exactly complete. Unknown frame values, displayed names ending in `...`, explicit markers and a missing final newline also create uncertainty. `--frame-cap-hint` can be changed only when you know the capture implementation's actual limit. This conservative policy trades some usable evidence for avoiding false certainty.

A truncated thread list makes missing-thread transitions uncertain. Complete stacks of threads that are still present can remain comparable. Transition labels mean newly observed or no longer observed in these captures; they do not assert actual thread creation/destruction.

## Output, budgets and statuses

JSON goes to stdout. Snapshot and line numbers are one-based. `streak_examples` contains the top frame and first/last header locations; `--include-frames` includes the entire displayed stack. Paths and function names can be sensitive. JSON escapes terminal control characters, but this is **not a redaction tool**.

`--samples 20` caps streak, transition and warning examples separately. Counts are exact across all inputs; streak examples are selected in run-completion order, so raise the limit when inspecting many threads. `snapshot_count`, coverage uncertainty and comparable-stack counts are always reported for every input. `sha256` identifies the bytes read, not their authenticity or provenance.

Defaults: `--max-bytes 8388608` per file, `--max-total-bytes 67108864`, `--max-snapshots 128`, `--max-threads 1024` per dump, `--max-frames 1000` per thread. Inputs and parsed stacks are held in memory within those budgets. A budget breach stops analysis; it never silently drops input. There is no duration/CPU estimate, lock ownership inference, automatic polling, repair or process restart.

Exit **0** = analyzed without evidence warnings (streaks are informational by default). **1** = evidence warnings, or a qualifying streak with `--fail-on-streak`. **2** = input/format/budget error with no partial success report. Redirect output to a path different from any input.

## Development and related tools

```sh
python -m unittest discover -s tests -v
python -m pip install ruff build
ruff check .
python -m build
python scripts/smoke_wheel.py
```

Tests use real `faulthandler` dumps from a controlled waiting thread and a deep recursive stack, alongside synthetic format/transition fixtures. CI covers Windows/Linux, Python 3.10/3.13. See [VALIDATION.md](VALIDATION.md).

[Python's documentation](https://docs.python.org/3/library/faulthandler.html) explains capture restrictions. [A joblib hang investigation](https://github.com/joblib/joblib/issues/1605) shows the practical value of these dumps, while [CPython issue 149085](https://github.com/python/cpython/issues/149085) describes thread-cap pitfalls. For live profiling, use [py-spy](https://github.com/benfred/py-spy). This tool focuses on already saved snapshots, consecutive equality and explicit uncertainty rather than live sampling or automated diagnosis.

## 中文说明

比较同一 Python 进程按顺序保存的 faulthandler 全线程快照，找出连续观察到的完整相同栈，提供快照编号及源文本行号。线程排列顺序变化不影响比较，缺失或截断则打断连续性。默认把达到 100 帧的栈视为可能截断，避免把不完整证据当作确定结果。相同栈不能证明死锁，也不代表进程没有进展；线程 ID 重用和两次采样之间的活动无法由文本确定。输出可能包含原始代码路径，请审阅后再分享。
