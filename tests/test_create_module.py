"""create_module offline (mocked Nemotron, real iverilog/vvp): spec ->
RTL -> testbench -> lint + simulation via verify_loop -> ONE pending diff
with every new file.

Cases: happy path; a debug round that fixes the RTL; the 3-round cap; one
approval prompt showing both new files ("no" -> repo byte-identical,
"yes" -> both created exactly); refusals (existing file, outside the repo,
"..", same path for both). Skipped when iverilog/vvp are not on PATH.
"""

import builtins
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot import cli  # noqa: E402
from src.copilot.tools import debugging, docs, generation, module_modifier, verify_loop  # noqa: E402

if not (shutil.which("iverilog") and shutil.which("vvp")):
    print("create_module: SKIPPED (iverilog/vvp not on PATH)")
    sys.exit(0)

SPEC = {
    "summary": "Register with enable.",
    "parameters": [{"name": "WIDTH", "default": "8", "description": "data width"}],
    "ports": [{"name": "clk", "direction": "input", "width": "1"}, {"name": "rst_n", "direction": "input", "width": "1"},
              {"name": "en", "direction": "input", "width": "1"}, {"name": "d", "direction": "input", "width": "WIDTH"},
              {"name": "q", "direction": "output", "width": "WIDTH"}],
    "clock": "clk", "reset": {"name": "rst_n", "active": "low", "type": "async"},
    "latency": "q updates one clock after en", "behavior": ["en loads d", "otherwise q holds"],
    "assumptions": ["Reset is asynchronous."],
}
RTL = """module dreg #(
  parameter WIDTH = 8
) (
  input  wire             clk,
  input  wire             rst_n,
  input  wire             en,
  input  wire [WIDTH-1:0] d,
  output reg  [WIDTH-1:0] q
);
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      q <= {WIDTH{1'b0}};
    end else if (en) begin
      q <= d;
    end
  end
endmodule
"""
RTL_BUG = RTL.replace("end else if (en) begin", "end else begin")   # ignores en
RTL_BUG2 = RTL.replace("q <= d;", "q <= ~d;")                       # still wrong
TB = """`timescale 1ns/1ps
module dreg_tb;
  reg clk = 0, rst_n = 0, en = 0;
  reg [7:0] d = 0;
  wire [7:0] q;
  reg [7:0] expected_q;
  integer checks = 0, wrong = 0;
  dreg #(.WIDTH(8)) dut (.clk(clk), .rst_n(rst_n), .en(en), .d(d), .q(q));
  always #5 clk = ~clk;
  task check(input [8*8:1] name);
    begin
      checks = checks + 1;
      if (q === expected_q) $display("PASS: %0s", name);
      else begin wrong = wrong + 1; $display("FAIL: %0s got=%h expected=%h", name, q, expected_q); end
    end
  endtask
  initial begin
    $dumpfile("dreg_tb.vcd");
    $dumpvars(0, dreg_tb);
    #12 expected_q = 8'h00; check("reset");
    rst_n = 1; d = 8'hA5; en = 1; @(posedge clk); #1 expected_q = 8'hA5; check("load");
    en = 0; d = 8'h3C; @(posedge clk); #1 expected_q = 8'hA5; check("hold");
    $display("DONE: %0d checks, %0d wrong", checks, wrong);
    $finish;
  end
  initial begin #10000 $display("FAIL: watchdog"); $finish; end
endmodule
"""
REQ = "A WIDTH-bit register: active-low reset clears q, en loads d, otherwise q holds."


class Model:
    def __init__(self, rtl, tb=(TB,), edits=(), debugs=(), audits=(), spec=SPEC, on_tb=None):
        self.rtl, self.tb, self.edits, self.debugs = list(rtl), list(tb), list(edits), list(debugs)
        self.audits, self.spec, self.on_tb = list(audits), spec, on_tb
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        system = kw["messages"][0]["content"]
        if system == docs._INTERFACE_SPEC_PROMPT:
            content = json.dumps(self.spec)
        elif system.startswith("You write synthesizable"):
            content = json.dumps({"verilog": self.rtl.pop(0)})
        elif system.startswith("You write a self-checking"):
            if self.on_tb:
                self.on_tb()
            content = json.dumps({"testbench": self.tb.pop(0)})
        elif system == debugging._AUDIT_PROMPT:
            content = self.audits.pop(0) if self.audits else json.dumps({"findings": []})
        elif system == debugging._DEBUG_PROMPT:
            content = self.debugs.pop(0)
        elif system == module_modifier._GEN_SYSTEM_PROMPT:
            content = json.dumps({"new_content": self.edits.pop(0), "explanation": "use en"})
        else:
            raise AssertionError(f"unexpected call: {system[:50]}")
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    def client(self):
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create)))


def debug_says_rtl() -> str:
    return json.dumps({"failing_check": "hold", "root_cause": "q follows d although en is low.", "file": "dreg.v",
                       "line": 12, "confidence": "high", "fix_instruction": "Only load q when en is high."})


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


work = Path(tempfile.mkdtemp(prefix="copilot_create_test_"))
module_modifier._PENDING_DIFFS_PATH = work / "pending.json"
made = []
_mkdtemp = tempfile.mkdtemp


def recording_mkdtemp(*a, **kw):
    p = _mkdtemp(*a, **kw)
    if kw.get("prefix") in ("copilot_verify_", "copilot_generate_"):
        made.append(Path(p))
    return p


def repo(name: str) -> Path:
    root = work / name
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "notes.md").write_text("# notes\n", encoding="utf-8")
    return root


def create(root: Path, model: Model, rtl_path="rtl/dreg.v", tb_path="tb/dreg_tb.v", check_tree=True):
    before = tree(root)
    made.clear()
    client = model.client()
    with mock.patch.object(docs, "get_client", return_value=client), \
         mock.patch.object(generation, "get_client", return_value=client), \
         mock.patch.object(debugging, "get_client", return_value=client), \
         mock.patch.object(module_modifier, "get_client", return_value=client), \
         mock.patch.object(verify_loop.tempfile, "mkdtemp", side_effect=recording_mkdtemp):
        r = generation.create_module(requirements=REQ, module_name="dreg", rtl_path=rtl_path, tb_path=tb_path,
                                     repo_root=str(root))
    assert tree(root) == before or not check_tree, "create_module never writes the repo"
    assert not any(p.exists() for p in made), "temp copy deleted"
    return r


try:
    # --- happy path ---
    root = repo("c1")
    m = Model([RTL])
    r = create(root, m)
    # spec, rtl, tb, audit, and the final re-audit of the finished testbench
    assert r["status"] == "passed" and len(r["attempts"]) == 1 and r["model_calls"] == 5, r
    assert r["final_check"] == {"interface": "matches the spec", "audit": "complete", "audit_findings": []}, r
    assert r["assumptions"] == ["Reset is asynchronous."] and r["rtl"]["interface"] == "matches the spec", r
    assert r["testbench"]["audit"] == "clean" and "dreg_tb.v: pass (3 checks)" in r["attempts"][0]["test_result"]
    assert r["pending_diff"]["files"] == ["rtl/dreg.v", "tb/dreg_tb.v"] == r["pending_diff"]["new_files"], r
    assert r["diff"].count("--- /dev/null") == 2, "both new files shown in full"
    assert "| Round | Change | Test result | Root cause if failed |" in r["attempts_table"]
    assert "copilot_verify_" not in json.dumps(r) and "ONE pending diff" in r["note"], r["note"]
    print("create_module: spec -> RTL -> testbench -> verified; ONE pending diff with both new files (5 calls): OK")

    # --- one approval prompt for both files ---
    diff_id = r["pending_diff"]["diff_id"]
    before = tree(root)
    assert module_modifier.apply_diff(diff_id)["status"] == "declined", "no approval, nothing created"
    out = io.StringIO()
    with mock.patch.object(builtins, "input", return_value="n"), mock.patch("sys.stdout", new=out):
        assert cli.confirm_tool_call("apply_diff", {"diff_id": diff_id}) is False
    shown = out.getvalue()
    assert shown.count("--- /dev/null") == 2 and "+module dreg #(" in shown and "+module dreg_tb;" in shown
    assert module_modifier.apply_diff(diff_id)["status"] == "declined" and tree(root) == before, "'no' changes nothing"
    with mock.patch.object(builtins, "input", return_value="y"), mock.patch("sys.stdout", new=io.StringIO()):
        assert cli.confirm_tool_call("apply_diff", {"diff_id": diff_id}) is True
    assert module_modifier.apply_diff(diff_id)["status"] == "ok"
    assert (root / "rtl/dreg.v").read_bytes() == RTL.encode() and (root / "tb/dreg_tb.v").read_bytes() == TB.encode()
    print("create_module: one prompt lists both files; 'no' -> byte-identical repo; 'yes' -> both created exactly: OK")

    # --- a debug round fixes the RTL ---
    root = repo("c2")
    m = Model([RTL_BUG, RTL], debugs=[debug_says_rtl()])  # the fix comes from the RTL generator itself
    r = create(root, m)
    assert r["status"] == "passed" and len(r["attempts"]) == 2, r.get("attempts")
    assert "dreg.v:12 (high)" in r["attempts"][0]["root_cause"] and r["attempts"][1]["change"].startswith("dreg.v:")
    assert "end else if (en) begin" in r["diff"], "the staged RTL is the fixed one"
    print("create_module: a failing first version is debugged and fixed in round 2: OK")

    # --- the 3-round cap ---
    root = repo("c3")
    m = Model([RTL_BUG, RTL_BUG2, RTL_BUG], debugs=[debug_says_rtl()] * 3)
    r = create(root, m)
    assert r["status"] == "still_failing" and r["final_status"] == "still failing after 3 round(s)", r
    assert r["pending_diff"] and "still fails" in r["note"], r
    print("create_module: stops at 3 rounds, still staged and flagged: OK")

    # --- an empty repo (the first module of a new project) works too ---
    root = work / "c_empty"
    root.mkdir()
    r = create(root, Model([RTL]))
    assert r["status"] == "passed" and r["pending_diff"]["new_files"] == ["rtl/dreg.v", "tb/dreg_tb.v"], r
    print("create_module: works in an empty repo: OK")

    # --- Codex review / live-run fixes ---
    # A serious finding in the FINAL audit means no plain "passed".
    root = repo("c5")
    serious = json.dumps({"findings": [{"id": "E3", "rtl_ref": "dreg.v:14", "explanation": "hold value wrong",
                                        "severity": "high"}]})
    r = create(root, Model([RTL], audits=[json.dumps({"findings": []}), serious]))
    assert r["status"] == "audit_findings" and r["final_check"]["audit_findings"], r
    # The testbench draft is compiled and repaired once before the loop's rounds start.
    root = repo("c6")
    broken = TB.replace('check("hold");', 'check("hold")')
    m = Model([RTL], tb=[broken, TB])
    r = create(root, m)
    assert r["status"] == "passed" and len(r["attempts"]) == 1 and r["model_calls"] == 6, r  # + one tb repair
    # A file that appears while this runs is never overwritten: nothing staged.
    root = repo("c7")
    r = create(root, Model([RTL], on_tb=lambda: ((root / "tb").mkdir(exist_ok=True),
                                                  (root / "tb" / "dreg_tb.v").write_text("// mine\n"))),
               check_tree=False)  # the test itself creates tb/dreg_tb.v mid-run
    assert r["status"] == "error" and "appeared in the repo" in r["message"] and not r.get("pending_diff"), r
    assert (root / "tb" / "dreg_tb.v").read_text() == "// mine\n"
    shutil.rmtree(root / "tb")
    # The compile-fix target is the file named by the FIRST error line, not a cascade.
    copy = verify_loop._Copy(work / "c6")
    a, b = work / "c6" / "x_tb.v", work / "c6" / "x.v"
    copy.edited = [b.resolve(), a.resolve()]
    stderr = f"{a}:141: syntax error\n{a}:141: error: Malformed statement\n{b}:13: error: Invalid module item.\n"
    assert verify_loop._compile_error_file(stderr, copy) == a.resolve()
    print("create_module: final re-audit gates 'passed'; tb compile repaired up front; files appearing mid-run "
          "never overwritten; compile fixes go to the first file blamed: OK")

    # Codex review, round 2: an existing testbench that already uses the new
    # module must pass too; a testbench fix that drops the DUT is refused.
    root = repo("c9")
    (root / "tb").mkdir()
    (root / "tb" / "user_tb.v").write_text(
        "module user_tb;\n  reg clk = 0, rst_n = 0, en = 0;\n  reg [7:0] d = 0;\n  wire [7:0] q;\n"
        "  dreg #(.WIDTH(8)) u (.clk(clk), .rst_n(rst_n), .en(en), .d(d), .q(q));\n"
        "  initial begin #1 if (q === 8'h00) $display(\"PASS: user reset\"); else $display(\"FAIL: user reset\");\n"
        "    $finish; end\nendmodule\n", encoding="utf-8")
    r = create(root, Model([RTL]))
    assert r["status"] == "passed" and "user_tb.v: pass (1 checks)" in r["attempts"][0]["test_result"], r["attempts"]
    root = repo("c10")
    tb_bug = TB.replace("#1 expected_q = 8'hA5; check(\"hold\");", "#1 expected_q = 8'h3C; check(\"hold\");")
    no_dut = TB.replace("  dreg #(.WIDTH(8)) dut (.clk(clk), .rst_n(rst_n), .en(en), .d(d), .q(q));\n", "")
    blame_tb = json.dumps({"failing_check": "hold", "root_cause": "expected value wrong", "file": "dreg_tb.v",
                           "line": 22, "confidence": "high", "fix_instruction": "expect A5"})
    r = create(root, Model([RTL], tb=[tb_bug, no_dut], debugs=[blame_tb]))
    assert r["status"] == "still_failing" and "no longer instantiates dreg" in r.get("stopped_early", ""), r
    print("create_module: existing testbenches of the new module must pass; DUT-less fixes refused: OK")

    # Coverage gate: a passing testbench must check every behaviour (+ reset).
    few = TB.replace('    en = 0; d = 8\'h3C; @(posedge clk); #1 expected_q = 8\'hA5; check("hold");\n', "")
    root = repo("c11")
    r = create(root, Model([RTL], tb=[few, TB]))
    assert r["status"] == "passed" and len(r["attempts"]) == 2, r["attempts"]
    assert "passes but is incomplete" in r["attempts"][0]["root_cause"] and "added checks" in r["attempts"][1]["change"]
    root = repo("c12")
    r = create(root, Model([RTL], tb=[few, few]))
    assert r["status"] == "tests_incomplete" and "don't yet check every behaviour" in r["note"], r
    print("create_module: too few checks -> testbench completed in round 2; never completed -> tests_incomplete: OK")

    # --- connect_to files in another folder are simulated too ---
    root = repo("c8")
    (root / "lib").mkdir()
    (root / "lib" / "ops.v").write_text("module adder (input wire [7:0] a, input wire [7:0] b, output wire [7:0] s);\n"
                                        "  assign s = a + b;\nendmodule\n", encoding="utf-8")
    sum_spec = dict(SPEC, parameters=[], assumptions=[], clock=None, reset=None, behavior=["s is a + b"], ports=[
        {"name": "a", "direction": "input", "width": "8"}, {"name": "b", "direction": "input", "width": "8"},
        {"name": "s", "direction": "output", "width": "8"}])
    wrap = ("module dreg (\n  input  wire [7:0] a,\n  input  wire [7:0] b,\n  output wire [7:0] s\n);\n"
            "  adder u_add (.a(a), .b(b), .s(s));\nendmodule\n")
    wrap_tb = ("module dreg_tb;\n  reg [7:0] a = 8'd2, b = 8'd3;\n  wire [7:0] s;\n  reg [7:0] expected_s;\n"
               "  dreg dut (.a(a), .b(b), .s(s));\n  initial begin\n    #1 expected_s = 8'd5;\n"
               "    if (s === expected_s) $display(\"PASS: sum\"); else $display(\"FAIL: sum got=%0d expected=%0d\", s, expected_s);\n"
               "    $display(\"DONE: 1 checks\");\n    $finish;\n  end\nendmodule\n")
    before = tree(root)
    made.clear()
    client = Model([wrap], tb=[wrap_tb], spec=sum_spec).client()
    with mock.patch.object(docs, "get_client", return_value=client), \
         mock.patch.object(generation, "get_client", return_value=client), \
         mock.patch.object(debugging, "get_client", return_value=client), \
         mock.patch.object(module_modifier, "get_client", return_value=client):
        r = generation.create_module(requirements="s = a + b using adder", module_name="dreg", rtl_path="rtl/dreg.v",
                                     tb_path="tb/dreg_tb.v", repo_root=str(root), connect_to=["lib/ops.v"])
    assert tree(root) == before and r["status"] == "passed", r
    print("create_module: connect_to modules in another folder are linted and simulated: OK")

    # --- refusals, before any model call ---
    root = repo("c4")
    (root / "rtl").mkdir()
    (root / "rtl" / "dreg.v").write_text("// mine\n", encoding="utf-8")
    for rtl_path, tb_path, why in (("rtl/dreg.v", "tb/dreg_tb.v", "already exists"),
                                   ("rtl/new.v", str(work / "x_tb.v"), "outside the repo root"),
                                   ("../new.v", "tb/n_tb.v", "'..'"), ("rtl/n.v", "rtl/n.v", "different files"),
                                   ("rtl/n.v", ".git/n_tb.v", ".git")):
        m = Model([])
        r = create(root, m, rtl_path=rtl_path, tb_path=tb_path)
        assert r["status"] == "error" and why in r["message"] and m.calls == [], (rtl_path, tb_path, r)
    print("create_module: existing files, outside/../.git paths and identical paths refused: OK")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\nCREATE MODULE TESTS PASSED")
