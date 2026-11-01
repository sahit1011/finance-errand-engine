"""The eval table must describe the whole suite, so only a run of every scenario may write it."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SENTINEL = "the table from the last full run\n"
RUN_TIMEOUT_SECONDS = 120


@pytest.fixture
def copy(tmp_path):
    """A copy of the tests, so these runs write their table there and never touch the real one."""
    shutil.copytree(REPO / "tests", tmp_path / "tests", ignore=shutil.ignore_patterns("__pycache__", "test_eval_table.py"))
    (tmp_path / "eval_results.md").write_text(SENTINEL)
    (tmp_path / "eval_results.json").write_text(SENTINEL)
    return tmp_path


def run(copy: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *args], cwd=copy, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS)


@pytest.mark.parametrize(
    "args",
    [
        ("tests/evals", "-k", "test_timeout_after_success"),
        ("tests/evals/test_slice1_booking.py",),
        ("tests/evals/test_slice2_safety.py::test_timeout_after_success",),
    ],
)
def test_running_only_some_scenarios_leaves_the_table_alone_and_says_so(copy, args):
    result = run(copy, *args)
    assert result.returncode == 0, result.stdout
    assert (copy / "eval_results.md").read_text() == SENTINEL
    assert (copy / "eval_results.json").read_text() == SENTINEL
    assert "eval table not written" in result.stdout


def test_a_run_with_no_terminal_output_still_finishes_cleanly(copy):
    # Not through run(), because -q comes from the terminal plugin this turns off.
    result = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-p", "no:terminal", "tests/evals/test_slice1_booking.py"], cwd=copy, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS)
    assert result.returncode == 0, result.stderr
    assert (copy / "eval_results.md").read_text() == SENTINEL


@pytest.mark.parametrize("broken", ["def broken(:\n", "import a_module_that_does_not_exist\n"])
def test_a_broken_file_elsewhere_does_not_fail_a_run_that_did_not_select_it(copy, broken):
    (copy / "tests" / "evals" / "test_slice3_actions.py").write_text(broken)
    result = run(copy, "tests/evals/test_slice1_booking.py")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr
    assert "eval table not written" in result.stdout
    assert (copy / "eval_results.md").read_text() == SENTINEL


def test_a_scenario_sharing_a_name_with_one_that_ran_still_counts_as_not_run(copy):
    (copy / "tests" / "evals" / "test_same_name.py").write_text(
        "from harness import scenario\n\n\n"
        '@scenario(99, "Same name", "Nothing")\n'
        "def test_happy_path_booking(env):\n"
        '    env.start()\n'
    )
    name = (copy / "tests" / "evals" / "test_slice1_booking.py").read_text()
    assert "def test_happy_path_booking(env)" in name, "the test needs a scenario with this name"
    result = run(copy, "tests/evals", "--deselect", "tests/evals/test_same_name.py::test_happy_path_booking")
    assert result.returncode == 0, result.stdout
    assert (copy / "eval_results.md").read_text() == SENTINEL


def test_stopping_early_leaves_the_table_alone(copy):
    bad = copy / "tests" / "evals" / "test_slice1_booking.py"
    bad.write_text(bad.read_text().replace('assert', 'assert False and', 1))
    result = run(copy, "tests/evals", "-x")
    assert result.returncode != 0
    assert (copy / "eval_results.md").read_text() == SENTINEL


def test_running_every_scenario_writes_the_table(copy):
    result = run(copy, "tests/evals")
    assert result.returncode == 0, result.stdout
    table = (copy / "eval_results.md").read_text()
    assert "scenarios passed" in table and SENTINEL not in table
    assert "eval table not written" not in result.stdout
    assert SENTINEL not in (copy / "eval_results.json").read_text()


def test_a_failing_scenario_in_a_full_run_is_recorded_as_a_failure(copy):
    bad = copy / "tests" / "evals" / "test_slice1_booking.py"
    bad.write_text(bad.read_text().replace('assert', 'assert False and', 1))
    result = run(copy, "tests/evals")
    assert result.returncode != 0
    assert "| FAIL |" in (copy / "eval_results.md").read_text()
