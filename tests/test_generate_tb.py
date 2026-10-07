"""generate_testbench offline (mocked Nemotron, real iverilog/vvp).

Cases: happy path (auditor clean, simulation passes, new-file diff); an
auditor finding fixed on retry; a testbench bug found by the debugger and
fixed; an RTL bug reported with file:line (testbench kept, RTL untouched);
the 3-round cap; unsafe testbench code never simulated or staged; a module
that only exists as a pending generate_rtl proposal; refusals (overwrite,
outside the repo, "..", .git); repo byte-identical; temp copy deleted.
Skipped when iverilog/vvp are not on PATH.
"""

import hashlib
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

from src.copilot.tools import debugging, generation, module_modifier  # noqa: E402

if not (shutil.which("iverilog") and shutil.which("vvp")):
    print("generate_testbench: SKIPPED (iverilog/vvp not on PATH)")
    sys.exit(0)

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
RTL_BUG = RTL.replace("end else if (en) begin", "end else begin")  # ignores en: hold breaks
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
    en = 0; d = 8'h3C; @(posedge clk); #1 expected_q = 8'hHOLD; check("hold");
    $display("DONE: %0d checks, %0d wrong", checks, wrong);
    $finish;
  end
  initial begin #10000 $display("FAIL: watchdog"); $finish; end
endmodule
"""
TB_GOOD = TB.replace("8'hHOLD", "8'hA5")
TB_WRONG = TB.replace("8'hHOLD", "8'h3C")  # the testbench's own expected value is wrong
TB_EVIL = TB_GOOD.replace('$dumpfile("dreg_tb.vcd");', '$dumpfile("dreg_tb.vcd"); $system("calc");')
HOLD_LINE = TB.splitlines().index(next(l for l in TB.splitlines() if "HOLD" in l)) + 1


def debug_says(file: str, line: int) -> str:
    return json.dumps({"failing_check": "hold", "root_cause": f"blamed {file}", "file": file, "line": line,
                       "confidence": "high", "fix_instruction": ""})


class Model:
    def __init__(self, tbs, audits=(), debugs=()):
        self.tbs, self.audits, self.debugs, self.calls = list(tbs), list(audits), list(debugs), []

    def create(self, **kw):
        self.calls.append(kw)
        system = kw["messages"][0]["content"]
        if system.startswith("You write a self-checking"):
            content = json.dumps({"testbench": self.tbs.pop(0), "notes": "ok"})
        elif system == debugging._AUDIT_PROMPT:
            content = self.audits.pop(0) if self.audits else json.dumps({"findings": []})
        elif system == debugging._DEBUG_PROMPT:
            content = self.debugs.pop(0)
        else:
            raise AssertionError(f"unexpected call: {system[:50]}")
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    def client(self):
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create)))


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


work = Path(tempfile.mkdtemp(prefix="copilot_gentb_test_"))
module_modifier._PENDING_DIFFS_PATH = work / "pending.json"
made = []
_mkdtemp = tempfile.mkdtemp


def recording_mkdtemp(*a, **kw):
    p = _mkdtemp(*a, **kw)
    if kw.get("prefix") == "copilot_generate_":
        made.append(Path(p))
    return p


def repo(name: str, rtl: str = RTL) -> Path:
    root = work / name
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "dreg.v").write_text(rtl, encoding="utf-8")
    return root


def run(root: Path, model: Model, module="rtl/dreg.v", target="tb/dreg_tb.v", **kw):
    before = tree(root)
    made.clear()
    client = model.client()
    with mock.patch.object(generation, "get_client", return_value=client), \
         mock.patch.object(debugging, "get_client", return_value=client), \
         mock.patch.object(generation.tempfile, "mkdtemp", side_effect=recording_mkdtemp):
        r = generation.generate_testbench(module_path=module, repo_root=str(root), target_path=target,
                                          requirements="Register: reset clears q; en loads d; otherwise q holds.", **kw)
    assert tree(root) == before, "generate_testbench never writes the repo"
    assert not any(p.exists() for p in made), "temp copy deleted"
    return r


try:
    # --- happy path ---
    root = repo("t1")
    m = Model([TB_GOOD])
    r = run(root, m)
    assert r["status"] == "passed" and len(r["rounds"]) == 1 and r["model_calls"] == 2, r  # testbench + auditor
    assert r["rounds"][0]["audit"] == "clean" and r["rounds"][0]["simulation"] == "pass (3 checks)", r["rounds"]
    assert r["diff"].startswith("--- /dev/null") and r["pending_diff"]["files"] == ["tb/dreg_tb.v"], r
    prompt = m.calls[0]["messages"]
    assert '$dumpfile("dreg_tb.vcd")' in prompt[0]["content"] and "module dreg #(" in prompt[1]["content"]
    assert "DONE: <checks> checks" in prompt[0]["content"], "summary line can't be miscounted as PASS/FAIL"
    print("generate_testbench: audited, simulated, passes; new-file diff staged: OK")

    # --- an auditor finding is fed back and fixed ---
    root = repo("t2")
    finding = json.dumps({"findings": [{"id": "E3", "rtl_ref": "dreg.v:14", "explanation": "hold must keep A5",
                                        "severity": "high"}]})
    m = Model([TB_WRONG, TB_GOOD], audits=[finding])
    r = run(root, m)
    assert r["status"] == "passed" and len(r["rounds"]) == 2 and r["rounds"][0]["audit"] == "1 issue(s)", r["rounds"]
    assert r["rounds"][0]["simulation"].startswith("FAIL") and "expected value wrong" in m.calls[2]["messages"][-1]["content"]

    # --- the debugger blames the testbench: fixed on retry ---
    root = repo("t3")
    m = Model([TB_WRONG, TB_GOOD], debugs=[debug_says("dreg_tb.v", HOLD_LINE)])
    r = run(root, m)
    assert r["status"] == "passed" and len(r["rounds"]) == 2, r["rounds"]
    assert r["rounds"][0]["errors"][0] == f"Testbench bug at dreg_tb.v:{HOLD_LINE}: blamed dreg_tb.v", r["rounds"][0]
    assert any(e.startswith("Simulation output: FAIL: hold") for e in r["rounds"][0]["errors"]), "failing lines fed back"
    assert "Timing discipline" in m.calls[0]["messages"][0]["content"]
    print("generate_testbench: auditor findings and debugger-found testbench bugs fixed on retry: OK")

    # --- the debugger blames the RTL: reported with file:line, RTL untouched, testbench kept ---
    root = repo("t4", RTL_BUG)
    m = Model([TB_GOOD], debugs=[debug_says("dreg.v", 12)])
    r = run(root, m)
    assert r["status"] == "rtl_suspect" and r["rtl_bug"]["file"] == "dreg.v" and r["rtl_bug"]["line"] == 12, r
    assert len(r["rounds"]) == 1 and r["pending_diff"]["files"] == ["tb/dreg_tb.v"] and "modify_module" in r["note"]
    assert (root / "rtl" / "dreg.v").read_text(encoding="utf-8") == RTL_BUG
    print("generate_testbench: an RTL bug is named with file:line; RTL untouched; testbench staged: OK")

    # --- the 3-round cap ---
    root = repo("t5")
    m = Model([TB_WRONG] * 3, debugs=[debug_says("dreg_tb.v", HOLD_LINE)] * 3)
    r = run(root, m)
    assert r["status"] == "still_failing" and len(r["rounds"]) == 3 and r["pending_diff"], r
    assert r["model_calls"] == 9 and "still fails" in r["note"], r["model_calls"]  # 3 x (tb + audit + debug)

    # --- unsafe testbench code: never simulated; staged only if it gets fixed ---
    root = repo("t6")
    r = run(root, Model([TB_EVIL, TB_GOOD]))
    assert r["status"] == "passed" and "$system" in r["rounds"][0]["errors"][0] and r["rounds"][0]["simulation"] == "not run"
    pending_before = set(module_modifier._load_pending())
    r = run(repo("t7"), Model([TB_EVIL] * 3))
    assert r["status"] == "blocked" and r["pending_diff"] is None and set(module_modifier._load_pending()) == pending_before
    # In the web demo (compile_check given) no waveform dump is asked for, or allowed.
    root = repo("t8")
    m = Model([TB_GOOD.replace('$dumpfile("dreg_tb.vcd");', "").replace("$dumpvars(0, dreg_tb);", ""), TB_GOOD])
    r = run(root, m, compile_check=lambda files: None)
    assert r["status"] == "passed" and "do not dump a waveform" in m.calls[0]["messages"][0]["content"]
    print("generate_testbench: stops at 3 rounds; unsafe code never run nor staged; no dumps in the web demo: OK")

    # --- live-run fixes: compile errors first (no auditor call), with a Verilog-2005 hint ---
    root = repo("t11")
    sv_in_v = TB_GOOD.replace("  task check(input [8*8:1] name);\n    begin\n",
                              "  task check(input [8*8:1] name);\n    begin\n      integer tmp;\n")
    m = Model([sv_in_v, TB_GOOD])
    r = run(root, m)
    # Repaired inside round 1 (one extra call), not a whole round lost.
    assert r["status"] == "passed" and len(r["rounds"]) == 1, r["rounds"]
    assert r["rounds"][0]["change"] == "first version + compile fix" and r["model_calls"] == 3, r  # tb, repair, audit
    repair = m.calls[1]["messages"][-1]["content"]
    assert "HINT: this file must be plain Verilog-2005" in repair and "integer tmp;" in repair, "numbered lines shown"
    # A testbench that never instantiates the module goes straight back (no sim, no debug call).
    root = repo("t11b")
    no_dut = TB_GOOD.replace("dreg #(.WIDTH(8)) dut (.clk(clk), .rst_n(rst_n), .en(en), .d(d), .q(q));", "")
    m = Model([no_dut, TB_GOOD])
    r = run(root, m)
    assert r["status"] == "passed" and "never instantiates dreg" in r["rounds"][0]["errors"][0], r["rounds"]
    assert r["rounds"][0]["simulation"] == "not run" and r["model_calls"] == 3, r  # tb, tb, audit
    # Medium findings are reported, not fed back; high ones are.
    root = repo("t12")
    medium = json.dumps({"findings": [{"id": "E1", "rtl_ref": "dreg.v:12", "explanation": "style nit",
                                       "severity": "medium"}]})
    r = run(root, Model([TB_GOOD], audits=[medium]))
    assert r["status"] == "passed" and r["rounds"][0]["audit"] == "clean, 1 minor note(s)", r["rounds"]
    assert r["rounds"][0]["audit_notes"] == ["dreg_tb.v:20: style nit"], r["rounds"][0]
    # Codex review: an incomplete audit is never a plain "passed".
    root = repo("t13")
    r = run(root, Model([TB_GOOD], audits=["{}"]))  # malformed review -> partial coverage
    assert r["status"] == "passed_audit_incomplete" and "audit was incomplete" in r["note"], r
    no_expected = TB_GOOD.replace("expected_q", "zz_q")  # nothing the auditor recognises as an expected value
    r = run(repo("t14"), Model([no_expected, TB_GOOD]))
    assert r["status"] == "passed" and "No expected values found" in r["rounds"][0]["errors"][0], r["rounds"]
    print("generate_testbench: compile errors first with hints; only high findings fed back; audit gaps reported: OK")

    # --- Codex review: the module's sibling RTL is found when the testbench lives in tb/ ---
    root = repo("t15")
    (root / "rtl" / "dreg.v").write_text(RTL.replace("module dreg", "module dreg_core"), encoding="utf-8")
    (root / "rtl" / "dreg_top.v").write_text(
        "module dreg #(\n  parameter WIDTH = 8\n) (\n  input  wire             clk,\n  input  wire             rst_n,\n"
        "  input  wire             en,\n  input  wire [WIDTH-1:0] d,\n  output wire [WIDTH-1:0] q\n);\n"
        "  dreg_core #(.WIDTH(WIDTH)) u_core (.clk(clk), .rst_n(rst_n), .en(en), .d(d), .q(q));\nendmodule\n",
        encoding="utf-8")
    r = run(root, Model([TB_GOOD]), module="rtl/dreg_top.v")
    assert r["status"] == "passed", r["rounds"]
    # ...and the strict guard: a repo file opening a file by a non-literal path is not run.
    root = repo("t16")
    (root / "rtl" / "dreg.v").write_text(RTL.replace("endmodule", '  parameter LOG = "q.log";\n  integer f;\n'
                                                     '  initial f = $fopen(LOG, "w");\nendmodule'), encoding="utf-8")
    r = run(root, Model([TB_GOOD] * 3))
    assert r["status"] == "blocked" and "non-literal path" in r["message"] and r["pending_diff"] is None, r
    print("generate_testbench: sibling RTL found from tb/; non-literal file paths never simulated: OK")

    # --- Codex review, round 2 ---
    # (1) an escaped dump path ("\\057" is "/") is not a safe local dump.
    root = repo("t17")
    escaped = TB_GOOD.replace('$dumpfile("dreg_tb.vcd");', '$dumpfile("\\057tmp/dreg_tb.vcd");')
    r = run(root, Model([escaped, TB_GOOD]))
    assert r["status"] == "passed" and "dumpfile" in r["rounds"][0]["errors"][0], r["rounds"]
    # (2) a compile "repair" that drops the DUT is not accepted.
    root = repo("t18")
    broken = TB_GOOD.replace("check(\"hold\");", "check(\"hold\")")
    m = Model([broken, no_dut, TB_GOOD])
    r = run(root, m)
    assert r["rounds"][0]["change"] == "first version" and r["rounds"][0]["simulation"] == "compile_error", r["rounds"]
    assert r["status"] == "passed" and len(r["rounds"]) == 2, r["rounds"]
    # (3) a diagnosis naming a file that isn't part of the design is no "RTL bug".
    root = repo("t19")
    m = Model([TB_WRONG, TB_GOOD], debugs=[debug_says("ghost.v", 3)])
    r = run(root, m)
    assert r["status"] == "passed" and "rtl_bug" not in r and len(r["rounds"]) == 2, r
    print("generate_testbench: escaped dump paths refused; repairs re-checked; unresolved culprits retried: OK")

    # --- a module that so far only exists as a pending generate_rtl proposal ---
    root = work / "t9"
    (root / "rtl").mkdir(parents=True)
    new_rtl = root / "rtl" / "dreg.v"
    rtl_id = module_modifier.stage_pending([(str(new_rtl), RTL)], creates={str(new_rtl)}, repo_root=str(root))
    r = run(root, Model([TB_GOOD]))
    assert r["status"] == "passed" and r["module_pending"] is True and not new_rtl.exists(), r
    module_modifier.discard_pending_diff(rtl_id)
    print("generate_testbench: works on a module that is still only a pending proposal: OK")

    # --- refused before any model call ---
    root = repo("t10")
    (root / "tb").mkdir()
    (root / "tb" / "dreg_tb.v").write_text("// mine\n", encoding="utf-8")
    for module, target, why in (("rtl/dreg.v", "tb/dreg_tb.v", "already exists"),
                                ("rtl/dreg.v", str(work / "elsewhere_tb.v"), "outside the repo root"),
                                ("rtl/dreg.v", "../x_tb.v", "'..'"), ("rtl/dreg.v", ".git/x_tb.v", ".git"),
                                ("rtl/nope.v", "tb/n_tb.v", "No module at"),
                                (str(work / "t1" / "rtl" / "dreg.v"), "tb/o_tb.v", "outside the repo root")):
        m = Model([])
        r = run(root, m, module=module, target=target)
        assert r["status"] == "error" and why in r["message"] and m.calls == [], (target, r)
    print("generate_testbench: overwrite, outside/../.git targets and missing/outside modules refused: OK")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\nGENERATE TESTBENCH TESTS PASSED")
