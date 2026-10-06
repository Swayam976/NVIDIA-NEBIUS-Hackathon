"""Runs testbench_runner and lint_checker against the tiny fixtures in
tests/fixtures/. Skips (exit 0) when iverilog/verilator aren't installed, so
it is safe to run anywhere; on a machine with the tools it proves the skills
don't return "unavailable".
"""

import sys
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.copilot.tools import TOOL_IMPLS, verification  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"

tb = TOOL_IMPLS["testbench_runner"](
    module_path=str(FIXTURES / "counter.v"), tb_path=str(FIXTURES / "counter_tb.v")
)
if tb["status"] == "unavailable":
    print("testbench_runner: SKIPPED ->", tb["message"])
else:
    assert tb["status"] == "pass", tb
    print("testbench_runner: OK ->", tb["summary"].splitlines()[0])

clean = TOOL_IMPLS["lint_checker"](file_path=str(FIXTURES / "counter.v"))
if clean["status"] == "unavailable":
    print("lint_checker: SKIPPED ->", clean["message"])
else:
    assert clean["status"] == "clean", clean
    dirty = TOOL_IMPLS["lint_checker"](file_path=str(FIXTURES / "width_mismatch.v"))
    assert dirty["status"] == "issues_found", dirty
    # Verilator 5 says WIDTHEXPAND, Verilator 4 (Debian 11 / Streamlit Cloud) says WIDTH.
    assert dirty["warning_count"] == 1 and "%Warning-WIDTH" in dirty["warnings"][0], dirty
    print("lint_checker: OK -> clean fixture clean; width warning caught:", dirty["warnings"][0][:70])

# --- Windows: never pick the extensionless `verilator` Perl script (WinError 193) ---
_fake_which = {"verilator": r"C:\fake\bin\verilator", "verilator_bin": r"C:\fake\bin\verilator_bin.EXE"}
with mock.patch.object(verification, "_IS_WINDOWS", True), mock.patch.object(
    verification.shutil, "which", side_effect=_fake_which.get
):
    exe, _env = verification._resolve_verilator()
assert exe.lower().endswith("verilator_bin.exe"), exe
print("_resolve_verilator on Windows prefers verilator_bin.exe: OK")

# --- a failed Verilator run must never be reported as "clean" ---
def _fake_lint(stderr, returncode):
    res = types.SimpleNamespace(stderr=stderr, returncode=returncode)
    with mock.patch.object(verification, "_resolve_verilator", return_value=("verilator", None)), mock.patch.object(
        verification.subprocess, "run", return_value=res
    ):
        return verification.lint_checker(file_path="x.v")


fault = _fake_lint("%Error: Exiting due to internal fault\n", 1)
assert fault["status"] == "issues_found" and "internal fault" in fault["warnings"][0], fault
crashed = _fake_lint("", 3)
assert crashed["status"] == "error", crashed
summary_only = _fake_lint("%Warning-WIDTHEXPAND: a.v:1:1: x\n%Error: Exiting due to 1 warning(s)\n", 1)
assert summary_only["warning_count"] == 1, summary_only
print("lint_checker failure handling (internal fault / nonzero exit): OK")

print("\nVERIFICATION TESTS DONE")
