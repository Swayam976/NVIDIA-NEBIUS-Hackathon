"""testbench_auditor offline (mocked Nemotron, no simulator needed).

Regression: tests/fixtures/riscv_alu/ holds verbatim copies of the real
RISC-V ALU testbench (with its known bug: shifts ctrl 5-7 use the full b
instead of b[4:0]) and ALU.v. The auditor must keep catching that bug on
lines 47-49, whatever the model says.
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

from src.copilot.tools import debugging  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "riscv_alu"
TB, RTL = FIX / "ALU_tb_shift_bug.v", FIX / "ALU.v"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def client(replies, calls):
    replies = list(replies)

    def create(**kw):
        calls.append(kw)
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        msg = types.SimpleNamespace(content=reply)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


def audit(replies, **kw):
    calls = []
    with mock.patch.object(debugging, "get_client", return_value=client(replies, calls)):
        return debugging.testbench_auditor(**{"tb_path": str(TB), "module_path": str(RTL), **kw}), calls


before = (sha(TB), sha(RTL))

# --- the known bug is caught by the rule even if the model reports nothing ---
r, calls = audit([json.dumps({"findings": []})])
assert r["status"] == "ok" and r["expected_values_checked"] == 11, r  # 10 case arms + default; $display text ignored
shift = [(f["line"], f["rtl_ref"], f["source"]) for f in r["findings"]]
assert shift == [(47, "ALU.v:34", "rule"), (48, "ALU.v:35", "rule"), (49, "ALU.v:36", "rule")], shift
assert all("full `b`" in f["explanation"] and "`b[4:0]`" in f["explanation"] for f in r["findings"])
assert all(f["file"] == "ALU_tb_shift_bug.v" for f in r["findings"])
assert len(calls) == 1 and calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
prompt = calls[0]["messages"][1]["content"]
assert "E6 (L47): 4'b0101: expected = a << b;" in prompt and "  34|" in prompt, "expected-value list + numbered RTL"
assert r["coverage"] == "complete" and r["context"]["omitted"] == [] and r["context"]["truncated"] == [], r
print("testbench_auditor: ALU_tb.v shift bug (ctrl 5-7, full b vs b[4:0]) caught on lines 47-49: OK")

# --- model findings: merged, validated, confirmations recorded ---
reply = json.dumps({"findings": [
    {"id": "E6", "rtl_ref": "ALU.v:34", "explanation": "same as rule", "severity": "high"},
    {"id": "e9", "rtl_ref": "ALU.v:37", "explanation": "invented for the test", "severity": "low"},
    {"id": "E99", "rtl_ref": "ALU.v:1", "explanation": "not an expected-value computation", "severity": "high"},
    {"tb_line": 42, "rtl_ref": "elsewhere.v:3", "explanation": "line-only ref to a file not shown", "severity": "bogus"},
]})
r, _ = audit([reply])
lines = {f["line"]: f for f in r["findings"]}
assert sorted(lines) == [42, 47, 48, 49, 50], sorted(lines)
assert lines[47]["confirmed_by_model"] and "confirmed_by_model" not in lines[48]
assert lines[50]["source"] == "model" and lines[50]["rtl_ref"] == "ALU.v:37" and lines[50]["severity"] == "low"
assert lines[42]["rtl_ref"] is None and lines[42]["severity"] == "medium", "unknown file / severity sanitised"
print("testbench_auditor: model findings merged, invalid lines dropped, refs validated: OK")

# --- outage: rule findings stand; reasoning cut off -> one retry without it ---
r, calls = audit([RuntimeError("down"), RuntimeError("down")])
assert [f["line"] for f in r["findings"]] == [47, 48, 49] and "unavailable" in r["model_review"], r
r, calls = audit([RuntimeError("cut off"), json.dumps({"findings": []})])
assert len(calls) == 2 and calls[1]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
print("testbench_auditor: outage keeps rule findings; one retry without reasoning: OK")

tmp = Path(tempfile.mkdtemp(prefix="copilot_audit_"))
try:
    # A fixed testbench has no shift findings.
    fixed = tmp / "ALU_tb_fixed.v"
    fixed.write_text(TB.read_text(encoding="utf-8").replace("a << b;", "a << b[4:0];")
                     .replace("a >> b;", "a >> b[4:0];").replace(">>> b;", ">>> b[4:0];"), encoding="utf-8")
    r, _ = audit([json.dumps({"findings": []})], tb_path=str(fixed))
    assert r["findings"] == [], r
    # A spec, when given, goes to the model with line numbers.
    spec = tmp / "rv32i_shifts.md"
    spec.write_text("SLL/SRL/SRA shift by rs2[4:0] (the low 5 bits).\n", encoding="utf-8")
    r, calls = audit([json.dumps({"findings": []})], spec_path=str(spec))
    assert f"=== FILE: {spec} ===" in calls[0]["messages"][1]["content"]
    # Nothing to audit -> no model call.
    empty = tmp / "plain_tb.v"
    empty.write_text('module plain_tb; initial $display("expected = %d", 1); endmodule\n', encoding="utf-8")
    r, calls = audit([], tb_path=str(empty))
    assert r["status"] == "no_expected_values" and calls == [], r
    assert debugging.testbench_auditor(tb_path=str(TB))["status"] == "error", "RTL is required"
    print("testbench_auditor: fixed tb clean, spec passed on, nothing-to-audit stops early: OK")

    # Codex review: (1) no borrowed RTL case, (2) multi-line / same-line
    # assignments, (3) /* */ comments are not code.
    rtl = tmp / "sh.v"
    rtl.write_text("module sh(input [31:0] a, b, input [1:0] op, output reg [31:0] y);\n"
                   "  always @(*) case (op)\n"
                   "    2'd0: y = a << b;\n"          # this RTL case does NOT slice
                   "    2'd1: y = a >> b[4:0];\n"     # another case does
                   "    default: y = 0;\n  endcase\nendmodule\n", encoding="utf-8")
    tbx = tmp / "sh_tb.v"
    tbx.write_text("module sh_tb; reg [31:0] a, b, expected, ref_y; reg [1:0] op; wire [31:0] y;\n"
                   "  sh dut(.a(a), .b(b), .op(op), .y(y));\n"
                   "  initial begin\n"
                   "    case (op)\n"
                   "      2'd0: expected = a << b;\n"                    # line 5: matches its RTL case -> no finding
                   "      2'd1: expected =\n          a >> b;\n"        # line 6: multi-line, RTL slices -> finding
                   "    endcase\n"
                   "    expected = a + 1; ref_y = a - 1;\n"             # line 9: two on one line
                   "    /* 2'd1: expected = a >> b;\n       ref_y = a >> b; */\n"  # lines 10-11: comment
                   "  end\nendmodule\n", encoding="utf-8")
    exp = debugging._expected_value_lines(tbx.read_text(encoding="utf-8"))
    assert [(e["line"], e["label"]) for e in exp] == [(5, "2'd0"), (6, "2'd1"), (9, None), (9, None)], exp
    assert exp[1]["rhs"] == "a >> b" and exp[1]["code"] == "2'd1: expected = a >> b;", exp[1]
    rule = debugging._shift_amount_findings(exp, [rtl])
    assert [(f["line"], f["rtl_ref"]) for f in rule] == [(6, "sh.v:4")], rule
    print("testbench_auditor: matching case required, multi-line + same-line assignments, comments skipped: OK")

    # Codex review, round 2:
    # (1) a case label on the PRECEDING line is recognised (both sides), and an
    #     unlabelled line whose RTL counterpart is ambiguous is not flagged.
    rtl2 = tmp / "sh2.v"
    rtl2.write_text("module sh2(input [31:0] a, b, input [1:0] op, output reg [31:0] y);\n"
                    "  always @(*) case (op)\n"
                    "    2'd0:\n      y = a << b;\n"          # unsliced arm, label on its own line
                    "    2'd1: begin\n      y = a << b[4:0];\n    end\n"
                    "    default: y = 0;\n  endcase\nendmodule\n", encoding="utf-8")
    tb2 = ("module t; reg [31:0] a, b, expected; reg [1:0] op;\n  initial begin\n    case (op)\n"
           "      2'd0:\n        expected = a << b;\n"         # line 5: matches its (unsliced) arm
           "      2'd1:\n        expected = a << b;\n"         # line 7: its arm slices -> finding
           "    endcase\n    expected = a << b;\n"              # line 9: unlabelled, RTL ambiguous -> none
           "  end\nendmodule\n")
    exp2 = debugging._expected_value_lines(tb2)
    assert [(e["line"], e["label"]) for e in exp2] == [(5, "2'd0"), (7, "2'd1"), (9, None)], exp2
    assert [(f["line"], f["rtl_ref"]) for f in debugging._shift_amount_findings(exp2, [rtl2])] == [(7, "sh2.v:6")]
    # (2) two computations on one line are merged by id, not by line number.
    tbx_text = tbx.read_text(encoding="utf-8")
    two = json.dumps({"findings": [
        {"id": "E4", "rtl_ref": "sh.v:3", "explanation": "ref_y model wrong", "severity": "medium"},
        {"tb_line": 9, "rtl_ref": "sh.v:3", "explanation": "ambiguous line-only finding", "severity": "high"},
    ]})
    r, _ = audit([two], tb_path=str(tbx), module_path=str(rtl))
    on9 = [(f["id"], f["source"]) for f in r["findings"] if f["line"] == 9]
    assert on9 == [("E4", "model")], (on9, tbx_text)
    # (3) sources the model could not fully see -> coverage reported as partial.
    big = tmp / "big.v"
    big.write_text(rtl.read_text(encoding="utf-8").replace("endmodule", "  // " + "x" * 20000 + "\n  wire w;\nendmodule"),
                   encoding="utf-8")
    r, _ = audit([json.dumps({"findings": []})], tb_path=str(tbx), module_path=str(big))
    assert r["coverage"] == "partial" and r["context"]["truncated"] == ["big.v"] and "partial check" in r["summary"], r
    print("testbench_auditor: preceding-line labels, per-computation ids, partial coverage reported: OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

assert (sha(TB), sha(RTL)) == before, "report only: nothing written"
print("\nAUDITOR TESTS PASSED")
