"""scripts/run-digest.sh, run for real against fakes.

The script is copied into a temporary project folder (it cds to the folder
above itself, so its log lands there), and desk-collect, osascript and open
are small fake scripts that record how they were called. No real run, no
pop-up on screen.

The pop-up runs in the background, so the script can end before the fake
osascript does: tests that look at the pop-up wait for it with `waited_for`.
"""

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "run-digest.sh"


def fake(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)


def waited_for(path: Path, seconds: float = 5) -> bool:
    """True once `path` exists, or False after `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


@dataclass
class Ran:
    code: int           # the script's exit code
    seconds: float      # how long the script took
    args: str           # the arguments desk-collect got
    log: str | None     # the log's text, or None if nothing was logged
    calls: Path         # where the fakes leave their marks

    def popup_shown(self) -> bool:
        return waited_for(self.calls / "popup-done")

    def opened(self, seconds: float = 5) -> str | None:
        """What `open` was given, or None if it wasn't called within `seconds`.
        It runs just after the pop-up answers, so wait for it, not for the pop-up."""
        path = self.calls / "open"
        return path.read_text().strip() if waited_for(path, seconds) else None


def run_script(tmp_path: Path, output: str, status: int = 0, *args: str,
               button: str = "Later", popup_seconds: float = 0) -> Ran:
    """Run run-digest.sh with a desk-collect that prints `output` and exits `status`.

    The fake pop-up stays up `popup_seconds`, then answers `button`."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, project / "scripts")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    calls.mkdir()
    (tmp_path / "output").write_text(output)
    fake(bin_dir, "desk-collect",
         f'echo "$@" > {calls}/args\ncat {tmp_path}/output\nexit {status}')
    fake(bin_dir, "osascript",
         f'sleep {popup_seconds}\necho "button returned:{button}"\ntouch {calls}/popup-done')
    fake(bin_dir, "open", f'echo "$@" > {calls}/open')

    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "DESK_COLLECT": str(bin_dir / "desk-collect"),
        "HOME": str(tmp_path),
    }
    start = time.monotonic()
    result = subprocess.run(
        [str(project / "scripts" / "run-digest.sh"), *args], env=env, timeout=30,
    )
    seconds = time.monotonic() - start
    log = project / "data" / "logs" / "desk-collect.log"
    return Ran(
        code=result.returncode,
        seconds=seconds,
        args=(calls / "args").read_text().strip(),
        log=log.read_text() if log.exists() else None,
        calls=calls,
    )


def a_page(tmp_path: Path, summary: str = "3 new items") -> Path:
    page = tmp_path / "page.html"
    page.write_text(f'<p class="meta">Fri Nov 20 · {summary}</p>\n')
    return page


# --- not due: stop quietly ----------------------------------------------------

def test_not_due_stops_quietly(tmp_path):
    ran = run_script(
        tmp_path, "# Not due: a page was already built after the latest slot\n", 0, "--if-due",
    )
    assert ran.code == 0
    assert ran.args == "--if-due"  # the script's arguments reach desk-collect
    assert ran.log is None         # no log entry
    assert not waited_for(ran.calls / "popup-done", seconds=0.5)  # no pop-up


def test_not_due_with_a_failed_exit_is_not_hidden(tmp_path):
    # Shouldn't happen, but if it did, a crash must still reach the log and a pop-up.
    ran = run_script(tmp_path, "# Not due: ...\nTraceback ...\n", 1, "--if-due")
    assert ran.code == 1
    assert "Traceback" in ran.log
    assert ran.popup_shown()


def test_not_due_later_in_the_output_is_not_a_skip(tmp_path):
    # Only output that starts with it counts: anything printed first (a warning)
    # is something to see in the log.
    ran = run_script(tmp_path, "Warning: something\n# Not due: ...\n", 0, "--if-due")
    assert ran.code == 0
    assert "Warning: something" in ran.log
    assert ran.popup_shown()


# --- held back: every source failed, no page -----------------------------------

HELD_BACK_OUTPUT = (
    "# 0 new event(s)\n"
    "# No page: every source failed, so the next check tries again\n"
    "# ERROR edgar CLSK: [Errno 8] nodename nor servname provided, or not known\n"
    "# ERROR finnhub_quotes CLSK: https://finnhub.io/api/v1/quote?symbol=CLSK&token=SECRET\n"
)


def test_held_back_logs_three_lines_and_shows_no_popup(tmp_path):
    ran = run_script(tmp_path, HELD_BACK_OUTPUT, 1, "--if-due")
    assert ran.code == 1  # still a failure, for launchd
    entry = ran.log.strip().splitlines()
    assert len(entry) == 3 and entry[0].startswith("=== ") and entry[0].endswith("exit 1")
    assert entry[1:] == [
        "# No page: every source failed, so the next check tries again",
        "# ERROR edgar CLSK: [Errno 8] nodename nor servname provided, or not known",
    ]
    assert not waited_for(ran.calls / "popup-done", seconds=0.5)


def test_held_back_log_is_redacted(tmp_path):
    # Here the first error is the one with the key in it.
    output = HELD_BACK_OUTPUT.replace("# ERROR edgar CLSK: [Errno 8] nodename nor servname provided, or not known\n", "")
    ran = run_script(tmp_path, output, 1)
    assert "token=REDACTED" in ran.log and "SECRET" not in ran.log


def test_no_page_with_exit_0_is_not_hidden(tmp_path):
    # Shouldn't happen (a held-back run always has errors), but if it did, show it.
    ran = run_script(tmp_path, "# No page: every source failed, so the next check tries again\n", 0)
    assert ran.popup_shown()


def test_no_page_inside_a_message_is_not_held_back(tmp_path):
    # Only a line that starts with it counts.
    page = a_page(tmp_path)
    output = f"# Digest page: {page}\n# ERROR digest page: # No page: weird message\n"
    ran = run_script(tmp_path, output, 1)
    assert f"# Digest page: {page}" in ran.log
    assert ran.popup_shown()


def test_held_back_matches_what_desk_collect_prints():
    # The script looks for these words: if collect.py's changed, this fails.
    from daily_digest_desk.collect import HELD_BACK
    assert f"grep -q '^{HELD_BACK}:'" in SCRIPT.read_text()


# --- a run: log it, show the pop-up -------------------------------------------

def test_a_run_with_a_page_is_logged_and_shown(tmp_path):
    page = a_page(tmp_path)
    ran = run_script(tmp_path, f"# Digest page: {page}\n", 0, "--if-due")
    assert ran.code == 0
    assert f"# Digest page: {page}" in ran.log
    assert ran.popup_shown()


def test_without_arguments_desk_collect_gets_none(tmp_path):
    page = a_page(tmp_path, "Nothing new since the last digest.")
    ran = run_script(tmp_path, f"# Digest page: {page}\n")
    assert ran.args == ""


def test_the_script_ends_without_waiting_for_the_popup(tmp_path):
    # A pop-up nobody clicks must not keep the job running: launchd wouldn't
    # start the next check until it ended.
    page = a_page(tmp_path)
    ran = run_script(tmp_path, f"# Digest page: {page}\n", popup_seconds=3)
    assert ran.seconds < 2
    assert ran.popup_shown()  # still shown, after the script ended


def test_open_opens_the_page(tmp_path):
    page = a_page(tmp_path)
    ran = run_script(tmp_path, f"# Digest page: {page}\n", button="Open", popup_seconds=1)
    assert ran.opened() == str(page)


def test_later_opens_nothing(tmp_path):
    page = a_page(tmp_path)
    ran = run_script(tmp_path, f"# Digest page: {page}\n", button="Later")
    assert ran.popup_shown()
    assert ran.opened(seconds=0.5) is None


def test_a_status_other_than_0_is_passed_on(tmp_path):
    page = a_page(tmp_path)
    ran = run_script(tmp_path, f"# Digest page: {page}\n# ERROR finnhub AAPL: ...\n", 1)
    assert ran.code == 1
