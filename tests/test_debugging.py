"""debug_failing_test offline (mocked Nemotron, real iverilog): a shifter whose
testbench has the same bug as the real ALU_tb.v (expected value shifts by
the full b instead of b[4:0]). Checks the evidence the model gets, the call
budget, the pending diff, that nothing is written, and the not-debuggable
paths. Skipped when iverilog/vvp are not on PATH.
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

from src.copilot.tools import debugging, module_modifier  # noqa: E402

if not (shutil.which("iverilog") and shutil.which("vvp")):
    print("debug_failing_test: SKIPPED (iverilog/vvp not on PATH)")
    sys.exit(0)

DESIGN = """module shifter(input [31:0] a, input [31:0] b, input op, output reg [31:0] y);
  always @(*) y = op ? (a >> b[4:0]) : (a << b[4:0]);
endmodule
"""
TB = """`timescale 1ns/1ps
module shifter_tb;
  reg [31:0] a, b, expected; reg op; wire [31:0] y; integer i;

  shifter dut (.a(a), .b(b), .op(op), .y(y));

  initial begin
    for (i = 0; i < 4; i = i + 1) begin
      a = 32'h0000_00f0 + i; b = (i < 2) ? i + 1 : 32 + i; op = i[0];
      #1;
      expected = op ? (a >> b) : (a << b);
      #1;
      if (y !== expected) $display("FAIL,a = %0d, b = %0d, op = %0d, y = %0d, expected = %0d", a, b, op, y, expected);
      else $display("PASS");
    end
    $finish;
  end
endmodule
"""
EXPECTED_LINE = 11  # the "expected = ..." line above
FIXED_TB = TB.replace("(a >> b) : (a << b)", "(a >> b[4:0]) : (a << b[4:0])").replace("\n\n  shifter dut", "\n  shifter dut")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def client(root_cause_replies, calls):
    """Root-cause call(s) answered from root_cause_replies in order; the
    modify_module call returns the fixed testbench (with a blank line dropped,
    which the whitespace guard must undo)."""
    replies = list(root_cause_replies)

    def create(**kw):
        calls.append(kw)
        system = kw["messages"][0]["content"]
        if system == module_modifier._GEN_SYSTEM_PROMPT:
            content = json.dumps({"new_content": FIXED_TB, "explanation": "Shift by b[4:0] like the RTL."})
        else:
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            content = reply
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


tmp = Path(tempfile.mkdtemp(prefix="copilot_debug_test_"))
try:
    design, tb = tmp / "shifter.v", tmp / "shifter_tb.v"
    design.write_text(DESIGN, encoding="utf-8")
    tb.write_text(TB, encoding="utf-8")
    module_modifier._PENDING_DIFFS_PATH = tmp / "pending.json"
    before = (sha(design), sha(tb))
    good = json.dumps({
        "failing_check": "b = 34: y = 0x3c, expected = 0",
        "root_cause": "The testbench shifts by the full b; the RTL uses b[4:0] per RV32I.",
        "file": str(tb), "line": EXPECTED_LINE, "confidence": "high",
        "fix_instruction": "Use b[4:0] as the shift amount in the expected-value line.",
    })

    def run(replies, **kw):
        calls = []
        with mock.patch.object(debugging, "get_client", return_value=client(replies, calls)), \
             mock.patch.object(module_modifier, "get_client", return_value=client([], calls)):
            return debugging.debug_failing_test(**{"tb_path": str(tb), "module_path": str(design), **kw}), calls

    r, calls = run([good])
    assert r["status"] == "failing" and r["confidence"] == "high", r
    assert Path(r["file"]) == tb and r["line"] == EXPECTED_LINE, r
    assert len(calls) == 2, "one root-cause call + one modify_module call"
    first = calls[0]
    assert first["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}} and first["max_tokens"] >= 16000
    prompt = first["messages"][1]["content"]
    assert "FAIL,a = 242, b = 34, op = 0" in prompt, "first failing check sent"
    assert f"=== FILE: {tb} ===" in prompt and f"{EXPECTED_LINE:4d}|       expected = op" in prompt, "line-numbered tb"
    assert "=== FILE: " + str(design) in prompt, "design source sent"
    ev = r["evidence"]
    # 3rd vector: inputs change at 4000 ps, expected is computed one #1 later,
    # so 5000 ps is the first time every value in the failing line matches.
    assert ev["failure_time"] == "5000 (1ps)", ev
    assert {"shifter_tb.a[31:0]", "shifter_tb.b[31:0]", "shifter_tb.y[31:0]", "shifter_tb.expected[31:0]"} <= {w["signal"] for w in ev["waveform"]}, ev
    assert "waveform around the first failure" in prompt.lower() and "shifter_tb.expected" in prompt
    diff = r["pending_diff"]["diff"]
    assert "+      expected = op ? (a >> b[4:0]) : (a << b[4:0]);" in diff, diff
    assert diff.count("\n-") == 1 + diff.count("\n---"), "only the expected line changes: no blank-line noise"
    assert (sha(design), sha(tb)) == before, "nothing written"
    assert r["pending_diff"]["diff_id"] in module_modifier._load_pending(), "fix waits in the pending store"
    assert "apply_diff" in r["note"]
    print("debug_failing_test: root cause file:line + confidence, VCD window, pending diff, nothing written: OK")

    # Reasoning call cut off -> exactly one retry without reasoning.
    r, calls = run([RuntimeError("cut off"), good])
    assert r["status"] == "failing" and r["pending_diff"] and len(calls) == 3, (r, len(calls))
    assert calls[1]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    # Both fail -> no diagnosis, evidence kept, no diff.
    r, calls = run([RuntimeError("x"), RuntimeError("y")])
    assert r["status"] == "error" and r["evidence"]["first_failure"] and len(calls) == 2, r
    # A file the model was not shown can't become a fix target.
    r, calls = run([json.dumps({**json.loads(good), "file": "C:/elsewhere/secret.v"})])
    assert r["pending_diff"] is None and "no file it was shown" in r["note"] and len(calls) == 1, r
    print("debug_failing_test: retry without reasoning, outage, and unknown-file cases: OK")

    # Codex review: the EARLIEST failure is debugged even without fields; log
    # values are read in the testbench's own radix; no location is claimed
    # from an incomplete or ambiguous match.
    tb.write_text(TB.replace('$display("FAIL,a = %0d, b = %0d, op = %0d, y = %0d, expected = %0d", a, b, op, y, expected);',
                             'begin $display("FAIL vector %0d", i); $display("FAIL,a = %h, b = %h, op = %0d, y = %h, expected = %h", a, b, op, y, expected); end'),
                  encoding="utf-8")
    r, calls = run([good])
    assert r["evidence"]["first_failure"] == "FAIL vector 2", r["evidence"]
    assert r["evidence"]["failure_time"] is None and "fewer than two" in r["evidence"]["waveform_note"], r["evidence"]
    assert debugging._field_radixes(tb.read_text(encoding="utf-8")) == {"a": 16, "b": 16, "op": 10, "y": 16, "expected": 16}
    assert debugging._parse_number("2d", 16) == 45 and debugging._parse_number("8'h0f", None) == 15
    assert debugging._parse_number("10", None) is None, "bare value without a known radix is never guessed"
    assert debugging._parse_number("xx", 16) is None and debugging._parse_number("3'b1x0", None) is None
    vcd_line = "FAIL,a = f2, b = 22, op = 0, y = 3c8, expected = 0"   # hex values, as logged with %h
    with tempfile.TemporaryDirectory() as d:
        from src.copilot.tools.verification import testbench_runner
        tb.write_text(TB, encoding="utf-8")
        run_res = testbench_runner(module_path=str(design), tb_path=str(tb), vcd_out=Path(d) / "w.vcd")
        loc, why = debugging._locate_failure(Path(d) / "w.vcd", vcd_line, "shifter_tb", {"a": 16, "b": 16, "op": 10, "y": 16, "expected": 16})
        assert loc and loc["time"] == 5000, (loc, why)
        loc, why = debugging._locate_failure(Path(d) / "w.vcd", vcd_line, "shifter_tb", {})
        assert loc is None and "can't be read unambiguously" in why, why
        loc, why = debugging._locate_failure(Path(d) / "w.vcd", "FAIL,a = 242, ghost = 1", "shifter_tb", {"a": 10, "ghost": 10})
        assert loc is None and "'ghost' is not a signal" in why, why
        # Two different signals named 'q' at the same depth (dut1.q, dut2.q): declined, not guessed.
        (Path(d) / "tie.vcd").write_text(
            "$timescale 1ps $end\n$scope module tb $end\n$var reg 8 ! a [7:0] $end\n"
            "$scope module dut1 $end\n$var wire 8 \" q [7:0] $end\n$upscope $end\n"
            "$scope module dut2 $end\n$var wire 8 # q [7:0] $end\n$upscope $end\n$upscope $end\n"
            "$enddefinitions $end\n#0\nb1 !\nb101 \"\nb101 #\n", encoding="utf-8")
        loc, why = debugging._locate_failure(Path(d) / "tie.vcd", "FAIL,a = 1, q = 5", "tb", {"a": 10, "q": 10})
        assert loc is None and "'q' matches several signals (tb.dut1.q[7:0], tb.dut2.q[7:0])" in why, why
    # The same name printed in two radixes is ambiguous -> left out.
    assert debugging._field_radixes('$display("a=%d"); $display("FAIL a=%h b=%h", a, b);') == {"b": 16}
    print("debug_failing_test: earliest failure kept, radix from format strings, no guessed locations: OK")

    # modify_module's whitespace guard: unrelated blank-line/whitespace edits
    # are undone, but spaces inside a string literal are a real change.
    keep = module_modifier._keep_original_whitespace
    orig = 'module m;\n  initial $display("a  b");\n  assign d = e;\n\nendmodule\n'
    assert keep(orig, 'module m;\n  initial $display("a b");\n  assign d   =  e;\nendmodule') == \
        'module m;\n  initial $display("a b");\n  assign d = e;\n\nendmodule\n'
    assert keep("a;\nb;\n", "a;\nx;\nb;\n") == "a;\nx;\nb;\n", "real insertions are kept"
    print("modify_module whitespace guard: noise undone, string contents and insertions kept: OK")

    # Passing test: say so, no model call.
    tb.write_text(FIXED_TB, encoding="utf-8")
    r, calls = run([])
    assert r["status"] == "pass" and calls == [], r
    # Not runnable (missing program file): no model call, cause stated.
    tb.write_text(TB.replace("initial begin\n", 'reg [7:0] m [0:1];\n  initial begin\n    $readmemh("nope.mem", m);\n', 1),
                  encoding="utf-8")
    r, calls = run([])
    assert r["status"] == "not_debuggable" and r["missing_data_files"] == ["nope.mem"] and calls == [], r
    print("debug_failing_test: passing and not-runnable tests stop before any model call: OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\nDEBUGGING TESTS PASSED")
