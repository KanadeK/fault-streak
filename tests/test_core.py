import contextlib
import faulthandler
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from fault_streak.cli import main
from fault_streak.core import InputError, audit_files, compare, parse_snapshot


def block(tid=1, lineno=10, function="wait", path="worker.py", current=False):
    return (
        f"{'Current thread' if current else 'Thread'} 0x{tid:08x} (most recent call first):\n"
        f'  File "{path}", line {lineno} in {function}\n'
    )


def snapshot(text, **kwargs):
    return parse_snapshot(text.encode(), **kwargs)


class CompareTests(unittest.TestCase):
    def test_unchanged_worker_changed_main(self):
        a = snapshot(block(1) + "\n" + block(2, 30, "main", current=True))
        b = snapshot(block(2, 31, "main", current=True) + "\n" + block(1))
        result = compare([a, b], include_frames=True)
        self.assertEqual(result["streak_count"], 1)
        self.assertEqual(result["streak_examples"][0]["thread_id"], "0x1")
        self.assertEqual(result["transition_counts"], {"changed": 1, "unchanged": 1})
        self.assertEqual(result["streak_examples"][0]["last_header_line"], 4)

    def test_maximal_runs(self):
        result = compare([snapshot(block(lineno=n)) for n in [1, 1, 2, 1, 1, 1]])
        self.assertEqual(result["streak_count"], 2)
        self.assertEqual(
            [(r["first_snapshot"], r["last_snapshot"]) for r in result["streak_examples"]],
            [(1, 2), (4, 6)],
        )

    def test_minimum_and_sample_limit(self):
        dumps = [snapshot(block(1) + "\n" + block(2))] * 3
        result = compare(dumps, min_streak=3, samples=1)
        self.assertEqual(result["streak_count"], 2)
        self.assertEqual(len(result["streak_examples"]), 1)
        self.assertEqual(result["streak_examples_omitted"], 1)
        self.assertEqual(compare(dumps, min_streak=4)["streak_count"], 0)

    def test_missing_breaks_continuity(self):
        result = compare([snapshot(block(1)), snapshot(block(2)), snapshot(block(1))])
        self.assertEqual(result["streak_count"], 0)
        self.assertEqual(result["transition_counts"]["no_longer_observed"], 2)

    def test_identity_uses_full_stack_not_top_only(self):
        a = snapshot(block() + '  File "caller.py", line 10 in a\n')
        b = snapshot(block() + '  File "caller.py", line 20 in b\n')
        self.assertEqual(compare([a, b])["streak_count"], 0)

    def test_zero_padded_thread_ids_normalized(self):
        a = snapshot(block(10))
        b = snapshot(block(10).replace("0x0000000a", "0xA"))
        self.assertEqual(compare([a, b])["streak_count"], 1)

    def test_global_truncation_exposes_uncertain_absence(self):
        a = snapshot(block(1) + "\n" + block(2))
        b = snapshot(block(1) + "\n...\n")
        result = compare([a, b])
        self.assertTrue(result["transition_examples"][0]["coverage_uncertain"])
        # A captured complete stack remains useful even when other threads were omitted.
        self.assertEqual(result["streak_count"], 1)

    def test_incomplete_stack_breaks_streak(self):
        complete = snapshot(block())
        truncated = snapshot(block() + "  ...\n")
        result = compare([complete, truncated, complete])
        self.assertEqual(result["streak_count"], 0)
        self.assertEqual(result["transition_counts"]["incomparable"], 2)

    def test_silent_frame_cap_is_conservative(self):
        text = block() + "".join('  File "w.py", line 3 in recurse\n' for _ in range(99))
        data = snapshot(text)
        self.assertFalse(data.threads["0x1"].comparable)
        self.assertEqual(data.warnings[0]["code"], "possible_frame_cap")
        self.assertEqual(compare([data, data])["streak_count"], 0)

    def test_clipped_strings_and_unknown_lines(self):
        for text in [
            block(path="long..."),
            block(function="long..."),
            block().replace("line 10", "line ???"),
            block().replace('"worker.py"', "???"),
        ]:
            with self.subTest(text=text):
                data = snapshot(text)
                self.assertFalse(data.threads["0x1"].comparable)

    def test_no_python_frame_and_gc(self):
        no_frame = "Thread 0x1 (most recent call first):\n  <no Python frame>\n"
        self.assertFalse(snapshot(no_frame).threads["0x1"].comparable)
        gc = snapshot(block().replace("  File ", "  Garbage-collecting\n  File "))
        self.assertEqual(gc.warnings[0]["code"], "garbage_collection_observed")
        self.assertTrue(gc.threads["0x1"].comparable)

    def test_missing_final_newline_is_uncertain(self):
        data = snapshot(block().rstrip("\n"))
        self.assertFalse(data.threads["0x1"].comparable)
        self.assertTrue(data.incomplete)

    def test_timeout_and_crlf_bom(self):
        raw = ("\ufeffTimeout (0:00:10)!\n" + block()).replace("\n", "\r\n").encode()
        self.assertTrue(parse_snapshot(raw).threads["0x1"].comparable)

    def test_displayed_escape_sequences_remain_literal(self):
        data = snapshot(block(path=r"C:\src\app\u4e00.py"))
        self.assertEqual(data.threads["0x1"].frames[0][0], r"C:\src\app\u4e00.py")

    def test_duplicate_and_mixed_dumps_rejected(self):
        cases = [
            block() + block(),
            "junk\n" + block(),
            block() + "C stack trace\n",
            "Thread 0x1 (most recent call first):\n",
            "Stack (most recent call first):\n",
            block() + "...\n" + block(2),
            block() + "Timeout (0:00:10)!\n",
            block() + '  ...\n  File "x.py", line 1 in f\n',
            "",
            "Timeout (0:00:10)!\n",
        ]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(InputError):
                snapshot(text)
        with self.assertRaises(InputError):
            parse_snapshot(b"\xff")

    def test_disjoint_current_threads_rejected(self):
        with self.assertRaises(InputError):
            snapshot(block(1, current=True) + "\n" + block(2, current=True))

    def test_parse_budgets(self):
        for kwargs in [{"max_bytes": 2}, {"max_threads": 1}, {"max_frames": 0}]:
            with self.assertRaises(InputError):
                snapshot(block(1) + "\n" + block(2), **kwargs)
        with self.assertRaises(InputError):
            snapshot(block() + '  File "x", line 1 in f\n', max_frames=1)

    def test_comparison_validation(self):
        data = snapshot(block())
        for dumps, kwargs in [
            ([data], {}),
            ([data, data], {"min_streak": 1}),
            ([data, data], {"samples": 0}),
        ]:
            with self.assertRaises(InputError):
                compare(dumps, **kwargs)

    def test_files_are_readonly_and_exit_codes_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a.txt", Path(directory) / "b.txt"
            a.write_bytes(block().encode())
            b.write_bytes(block().encode())
            original = a.read_bytes()
            report = audit_files([a, b])
            self.assertEqual(report["streak_count"], 1)
            self.assertEqual(a.read_bytes(), original)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main([str(a), str(b)]), 0)
                self.assertEqual(main([str(a), str(b), "--fail-on-streak"]), 1)
            for extra in [
                ["--max-bytes", "1"],
                ["--max-total-bytes", "1"],
                ["--min-streak", "1"],
                ["--max-snapshots", "1"],
            ]:
                with contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(main([str(a), str(b), *extra]), 2)
            run = subprocess.run(
                [sys.executable, "-m", "fault_streak", str(a), str(b), "--fail-on-streak"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(run.returncode, 1)
            self.assertEqual(json.loads(run.stdout)["streak_count"], 1)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main([str(a), str(b) + ".absent"]), 2)

    def test_real_waiting_thread_snapshots(self):
        gate = threading.Event()
        started = threading.Event()

        def waiting_worker():
            started.set()
            gate.wait(10)

        worker = threading.Thread(target=waiting_worker)
        worker.start()
        try:
            self.assertTrue(started.wait(2))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                frame = sys._current_frames().get(worker.ident)
                if frame and frame.f_code.co_name == "wait":
                    break
                time.sleep(0.001)
            else:
                self.fail("controlled thread never entered Event.wait")
            with tempfile.TemporaryDirectory() as directory:
                paths = []
                for number in range(2):
                    path = Path(directory) / f"{number}.txt"
                    with path.open("wb") as handle:
                        faulthandler.dump_traceback(file=handle, all_threads=True)
                    paths.append(path)
                report = audit_files(paths)
                ids = [item["thread_id"] for item in report["streak_examples"]]
                self.assertIn(hex(worker.ident), ids)
        finally:
            gate.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_real_deep_stack_excluded(self):
        with tempfile.TemporaryFile() as handle:

            def descend(depth):
                if depth:
                    descend(depth - 1)
                else:
                    faulthandler.dump_traceback(file=handle, all_threads=True)

            descend(120)
            handle.seek(0)
            data = parse_snapshot(handle.read())
            self.assertTrue(any(w["code"] == "possible_frame_cap" for w in data.warnings))
            self.assertFalse(data.threads[hex(threading.get_ident())].comparable)


if __name__ == "__main__":
    unittest.main()
