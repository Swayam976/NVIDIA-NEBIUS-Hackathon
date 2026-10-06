"""Fixes from the live skill audit (TASKS.md #11/#12), no network:
testbench log summary, hazard checker rules + model-review handling,
RTL-grounded spec drafting, git errors surfaced + file-time fallback.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot.config import settings  # noqa: E402
from src.copilot.tools import docs, verification, workflow  # noqa: E402


def fake_client(reply=None, error=None, sink=None):
    def create(**kwargs):
        if sink is not None:
            sink.append(kwargs)
        if error:
            raise error
        msg = types.SimpleNamespace(content=reply)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


# --- 1. testbench log summary -------------------------------------------------
log = "\n".join(
    [f"FAIL,a = {1000 + i}, b = {4000 + 7 * i}, ctrl = {5 + i % 3}, result = {i}, expected = 0" for i in range(9)]
    + ["PASS"] * 20
)
s = verification._summarize_log(log)
assert s["pass_lines"] == 20 and s["fail_lines"] == 9, s
assert s["failure_fields"]["ctrl"] == {"5": 3, "6": 3, "7": 3}, s["failure_fields"]
assert "a" not in s["failure_fields"] and "b" not in s["failure_fields"], "operand noise must be dropped"
assert s["failure_fields"]["expected"] == {"0": 9}
assert len(s["first_failures"]) == 8
assert s["headline"].startswith("9 failing line(s), 20 passing line(s).") and "failures by ctrl: 5 (3x)" in s["headline"]
assert "RTL or in the testbench" in s["headline"], "hint must stay neutral (the demo's bug is in the RTL)"
# One real mismatch + a summary line: nothing to group, no misleading pattern.
s = verification._summarize_log("mismatch op=3'b100 a=8'haa: y=8'h00\nFAIL: 1 ALU check(s) failed\n")
assert s["fail_lines"] == 2 and s["failure_fields"] == {} and "Pattern" not in s["headline"], s
print("testbench summary: counts, grouping, neutral headline, no pattern from one line: OK")

# Status and counts must agree (Codex review): MISMATCH then PASS is a failure.
if shutil.which("iverilog") and shutil.which("vvp"):
    _tb_dir = Path(tempfile.mkdtemp(prefix="copilot_skills_"))
    try:
        (_tb_dir / "m.v").write_text("module m; endmodule\n", encoding="utf-8")
        (_tb_dir / "t.v").write_text(
            'module t; initial begin $display("mismatch x=1"); $display("PASS"); $finish; end endmodule\n',
            encoding="utf-8")
        r = verification.testbench_runner(str(_tb_dir / "m.v"), str(_tb_dir / "t.v"))
        assert r["status"] == "fail_or_unknown" and r["fail_lines"] == 1 and r["pass_lines"] == 1, r
    finally:
        shutil.rmtree(_tb_dir, ignore_errors=True)
    print("testbench status agrees with counts (mismatch + PASS -> fail): OK")

# --- 2. hazard checker ----------------------------------------------------------
REGRESSION = """--- a/forward_unit.v
+++ b/forward_unit.v
@@ -27,9 +27,7 @@
     always @ (*) begin
-        if (ex_mem_write && (ex_mem_rd != 0) && (ex_mem_rd == id_ex_rs1_rd))
-            forward_a = 2'b10;
-        else if (mem_wb_write && (mem_wb_rd != 0) && (mem_wb_rd == id_ex_rs1_rd))
+        if (mem_wb_write && (mem_wb_rd != 0) && (mem_wb_rd == id_ex_rs1_rd))
             forward_a = 2'b01;
"""
BENIGN = """--- a/forward_unit.v
+++ b/forward_unit.v
@@ -1,3 +1,3 @@
-    // forwarding unit v1
+    // forwarding unit v2
-            forward_a = 2'b10;
+            forward_a  =  2'b10;
"""
r = verification.hazard_sanity_checker(REGRESSION, llm_review=False)
assert r["status"] == "flagged" and any("ex_mem_rd == id_ex_rs1_rd" in f for f in r["flags"]), r
assert not any("mem_wb_rd == id_ex_rs1_rd" in f for f in r["flags"]), "re-added else-if must not be flagged"
r = verification.hazard_sanity_checker(BENIGN, llm_review=False)
assert r["status"] == "no_flags_needs_simulation" and r["flags"] == [], r
# Re-adding the same line in a *different* file must not hide its removal here.
MOVED = REGRESSION + """--- a/other_unit.v
+++ b/other_unit.v
@@ -1,1 +1,2 @@
+        if (ex_mem_write && (ex_mem_rd != 0) && (ex_mem_rd == id_ex_rs1_rd))
"""
r = verification.hazard_sanity_checker(MOVED, llm_review=False)
assert any("ex_mem_rd == id_ex_rs1_rd" in f for f in r["flags"]), r
print("hazard rules: removed EX/MEM forwarding flagged, comment/whitespace edit not: OK")

calls = []
review = json.dumps({"verdict": "likely_hazard", "findings": [{"line": "-x", "issue": "EX/MEM priority lost", "severity": "high"}]})
with mock.patch.object(verification, "get_client", return_value=fake_client(review, sink=calls)):
    r = verification.hazard_sanity_checker(BENIGN)
assert r["status"] == "flagged" and r["model_review"]["verdict"] == "likely_hazard", r
assert calls[0]["response_format"] == {"type": "json_object"}
with mock.patch.object(verification, "get_client", return_value=fake_client(json.dumps({"verdict": "no_hazard_found", "findings": []}))):
    r = verification.hazard_sanity_checker(BENIGN)
assert r["status"] == "no_flags_needs_simulation", "clean review must still say 'needs simulation', never 'safe'"
with mock.patch.object(verification, "get_client", return_value=fake_client(error=ConnectionError("down"))):
    r = verification.hazard_sanity_checker(REGRESSION)
assert r["status"] == "flagged" and r["model_review"]["status"] == "unavailable", r
for bad in ("not json", "[]", "null", '"text"'):  # invalid or non-object JSON
    with mock.patch.object(verification, "get_client", return_value=fake_client(bad)):
        r = verification.hazard_sanity_checker(REGRESSION)
    assert r["model_review"]["status"] == "unavailable" and r["status"] == "flagged", (bad, r)
print("hazard model review: flags, clean, outage and bad JSON all handled: OK")

# --- shared temp project (not inside any git repo) --------------------------------
tmp = Path(tempfile.mkdtemp(prefix="copilot_skills_"))
try:
    proj = tmp / "proj"
    (proj / "proj.srcs" / "sources_1" / "new").mkdir(parents=True)
    (proj / "proj.srcs" / "sim_1" / "new").mkdir(parents=True)
    (proj / "proj.cache" / "ip").mkdir(parents=True)
    src = proj / "proj.srcs" / "sources_1" / "new"
    (src / "hazard_unit.v").write_text(
        "// module fake_in_comment(input x);\nmodule hazard_unit(input [4:0] rs1, input load_ex, output stall);\nendmodule\n",
        encoding="utf-8")
    (src / "legacy.v").write_text(
        "module legacy(clk, q);\n  input clk;\n  output [3:0] q;\n"
        "  task load;\n    input [7:0] task_arg;\n    begin end\n  endtask\nendmodule\n", encoding="utf-8")
    (proj / "proj.srcs" / "sim_1" / "new" / "hazard_unit_tb.v").write_text("module hazard_unit_tb; endmodule\n", encoding="utf-8")
    (proj / "proj.cache" / "ip" / "gen.v").write_text("module generated_ip(input a); endmodule\n", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(src / "legacy.v", (old, old))

    # --- 3. grounded spec drafting ----------------------------------------------
    calls = []
    with mock.patch.dict(settings.project_repo_paths, {"riscv-core": str(proj)}), \
         mock.patch.object(docs, "get_client", return_value=fake_client("draft", sink=calls)):
        r = docs.spec_drafting_assistant("riscv-core", "hazard handling")
    prompt = calls[0]["messages"][1]["content"]
    system = calls[0]["messages"][0]["content"]
    assert "hazard_unit(input [4:0] rs1, input load_ex, output stall)" in prompt, prompt
    assert "legacy(input clk; output [3:0] q;)" in prompt, "Verilog-1995 ports must be picked up"
    assert "hazard_unit_tb" not in prompt and "generated_ip" not in prompt and "fake_in_comment" not in prompt
    assert prompt.index("hazard_unit(") < prompt.index("legacy("), "hint-relevant modules first"
    assert "TBD" in system and r["grounded_on_modules"] == ["hazard_unit", "legacy"], r
    assert "task_arg" not in prompt, "task/function inputs are not module ports"
    from src.copilot.tools.rtl_files import module_interfaces
    (src / "huge.v").write_text("module huge_hazard(" + ", ".join(f"input p{i}" for i in range(400)) + ");\nendmodule\n",
                                encoding="utf-8")
    _, names = module_interfaces(proj, hint="hazard", budget=300)
    assert "huge_hazard" not in names and "hazard_unit" in names, "an oversized entry must not stop collection"
    (src / "huge.v").unlink()
    calls = []
    with mock.patch.dict(settings.project_repo_paths, {"riscv-core": str(tmp / "missing")}), \
         mock.patch.object(docs, "get_client", return_value=fake_client("draft", sink=calls)):
        r = docs.spec_drafting_assistant("riscv-core", "hazard handling")
    assert "do not name any modules or signals" in calls[0]["messages"][1]["content"]
    assert r["grounded_on_modules"] == [] and "memory status only" in r["note"]
    print("spec drafting: real interfaces in prompt (tb/generated/comments excluded), no-RTL fallback: OK")

    # --- 4. git skills -----------------------------------------------------------
    r = workflow.commit_to_summary(str(proj))
    assert r["status"] == "error" and "not a git repository" in r["message"].lower(), r
    r = workflow.regression_spotter(str(proj), since="3.days")
    assert r["source"].startswith("file modification times"), r
    assert r["flagged"] == [{"changed_file": "proj.srcs/sources_1/new/hazard_unit.v",
                             "likely_tests": ["proj.srcs/sim_1/new/hazard_unit_tb.v"]}], r
    assert r["changed_count"] == 1, "legacy.v is older than the window; generated files are skipped"
    assert workflow.regression_spotter(str(proj), since="60.days")["changed_count"] == 2
    assert workflow.regression_spotter(str(proj), since="last tuesday")["status"] == "error"
    print("git skills, non-git folder: commit_to_summary errors; regression_spotter uses file times: OK")

    if shutil.which("git"):
        g = ["git", "-C", str(proj), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g[:3] + ["init", "-q"], check=True)
        subprocess.run(g + ["add", "-A"], check=True)
        subprocess.run(g + ["commit", "-qm", "init"], check=True)
        r = workflow.regression_spotter(str(proj), since="1.day")
        assert r["source"] == "git history" and r["changed_count"] == 2, r  # tb + generated excluded, as above
        print("git skills, real repo: regression_spotter uses git history: OK")
finally:
    # git marks object files read-only, which plain rmtree can't delete on Windows.
    shutil.rmtree(tmp, onexc=lambda fn, p, exc: (os.chmod(p, 0o700), fn(p)))

print("\nSKILL FIX TESTS PASSED")
