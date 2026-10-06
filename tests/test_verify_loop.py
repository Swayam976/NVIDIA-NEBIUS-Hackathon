"""verify_loop offline (mocked Nemotron, real iverilog): a small project with
an ALU, two testbenches that instantiate it, one that reaches it through a
top module, and one unrelated testbench. Goal: add XOR at op 2'd2.

Cases: passes in round 1; needs a debug round; needs a compile-fix round;
hits the 3-round cap; a "no" at the gate leaves the real files
byte-identical; a "yes" applies the combined diff all-or-nothing. Every case
checks the temp copy is gone and the real files are untouched by the loop.
Skipped when iverilog/vvp are not on PATH.
"""

import builtins
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot import cli  # noqa: E402
from src.copilot.tools import debugging, module_modifier, verify_loop as vl_mod  # noqa: E402

if not (shutil.which("iverilog") and shutil.which("vvp")):
    print("verify_loop: SKIPPED (iverilog/vvp not on PATH)")
    sys.exit(0)

ALU = """module alu(input [7:0] a, input [7:0] b, input [1:0] op, output reg [7:0] y);
  always @(*) begin
    case (op)
      2'd0: y = a + b;
      2'd1: y = a - b;
      default: y = 8'd0;
    endcase
  end
endmodule
"""
SUB_LINE = "      2'd1: y = a - b;\n"


def alu_with(op2: str) -> str:
    return ALU.replace(SUB_LINE, SUB_LINE + f"      2'd2: y = {op2};\n")


GOOD, OR_BUG, AND_BUG, SUB_BUG = alu_with("a ^ b"), alu_with("a | b"), alu_with("a & b"), alu_with("a - b")
SYNTAX_BUG = GOOD.replace("y = a ^ b;", "y = a ^ b")

TB = """`timescale 1ns/1ps
module {name};
  reg [7:0] a, b; reg [1:0] op; wire [7:0] y;
  {dut} dut(.a(a), .b(b), .op(op), .y(y));
  task check(input [7:0] exp);
    begin
      #1;
      if (y === exp) $display("PASS op=%0d", op);
      else $display("FAIL op=%0d y=%h expected=%h", op, y, exp);
    end
  endtask
  initial begin
    a = 8'hF0; b = 8'h3C;
    op = 0; check(8'h2C);
{extra}    $finish;
  end
endmodule
"""
ALU_TB = TB.format(name="alu_tb", dut="alu", extra="    op = 1; check(8'hB4);\n")
ALU_TB_NEW = TB.format(name="alu_tb", dut="alu", extra="    op = 1; check(8'hB4);\n    op = 2; check(8'hCC);\n")
FILES = {
    "rtl/alu.v": ALU,
    "rtl/top.v": "module top(input [7:0] a, input [7:0] b, input [1:0] op, output [7:0] y);\n"
                 "  alu u_alu(.a(a), .b(b), .op(op), .y(y));\nendmodule\n",
    "rtl/inv.v": "module inv(input a, output y);\n  assign y = ~a;\nendmodule\n",
    "sim/alu_tb.v": ALU_TB,
    "sim/alu2_tb.v": TB.format(name="alu2_tb", dut="alu", extra=""),
    "sim/top_tb.v": TB.format(name="top_tb", dut="top", extra=""),
    "sim/inv_tb.v": "module inv_tb; reg a; wire y; inv dut(.a(a), .y(y));\n"
                    "  initial begin a = 0; #1 if (y) $display(\"PASS\"); else $display(\"FAIL\"); $finish; end\nendmodule\n",
}


def debug_reply(fix: str = "Make op 2'd2 compute a ^ b.") -> str:
    return json.dumps({"failing_check": "op=2", "root_cause": "op 2'd2 does not compute XOR.", "file": "alu.v",
                       "line": 6, "confidence": "high", "fix_instruction": fix})


class Model:
    """Scripted Nemotron: edit calls answer from per-file queues (an empty
    queue returns the file unchanged), debug calls from their own queue."""

    def __init__(self, edits: dict[str, list[str]], debug: list[str] = ()):
        self.edits = {k: list(v) for k, v in edits.items()}
        self.debug = list(debug)
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        system, user = kw["messages"][0]["content"], kw["messages"][1]["content"]
        if system == module_modifier._GEN_SYSTEM_PROMPT:
            name = Path(re.match(r"FILE: (.*)", user).group(1).strip()).name
            current = user.split("CURRENT CONTENT:\n", 1)[1].rsplit("\n\nINSTRUCTION:\n", 1)[0]
            queue = self.edits.get(name) or []
            content = json.dumps({"new_content": queue.pop(0) if queue else current, "explanation": f"edit {name}"})
        elif system == debugging._DEBUG_PROMPT:
            content = self.debug.pop(0)
        else:
            raise AssertionError(f"unexpected model call: {system[:60]}")
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    def client(self):
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create)))


def sha_tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


work = Path(tempfile.mkdtemp(prefix="copilot_vl_test_"))
module_modifier._PENDING_DIFFS_PATH = work / "pending.json"
made_temps: list[Path] = []
_real_mkdtemp = tempfile.mkdtemp


def recording_mkdtemp(*a, **kw):
    p = _real_mkdtemp(*a, **kw)
    if kw.get("prefix") == "copilot_verify_":
        made_temps.append(Path(p))
    return p


def project(name: str) -> Path:
    root = work / name
    for rel, text in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def loop(root: Path, model: Model, **kw):
    before = sha_tree(root)
    made_temps.clear()
    client = model.client()
    with mock.patch.object(module_modifier, "get_client", return_value=client), \
         mock.patch.object(debugging, "get_client", return_value=client), \
         mock.patch.object(vl_mod.tempfile, "mkdtemp", side_effect=recording_mkdtemp):
        r = vl_mod.verify_loop(goal="Add XOR at op 2'd2.", module_path=str(root / "rtl/alu.v"),
                               tb_path=str(root / "sim/alu_tb.v"), **kw)
    assert sha_tree(root) == before, "the loop never writes the real files"
    assert r["temp_copy_removed"] and made_temps and not any(p.exists() for p in made_temps), "temp copy deleted"
    assert all(a["model_calls"] <= 4 for a in r.get("attempts", [])), r.get("attempts")
    return r


try:
    # The loop's own instructions must not switch off modify_module's
    # whitespace guard (found live: "same style" did, and a blank line was dropped).
    for text in (vl_mod._RTL_INSTRUCTION, vl_mod._TB_INSTRUCTION, vl_mod._COMPILE_FIX_INSTRUCTION,
                 vl_mod._LINT_FIX_INSTRUCTION):
        filled = text.format(goal="add a NOR op", module="rtl/alu.v", diff="+x", stderr="err", errors="e")
        assert not module_modifier._WHITESPACE_ASK_RE.search(filled), filled

    # --- 1. passes in round 1 ---
    root = project("p1")
    m = Model({"alu.v": [GOOD], "alu_tb.v": [ALU_TB_NEW]})
    r = loop(root, m)
    assert r["status"] == "passed" and r["final_status"] == "passed" and len(r["attempts"]) == 1, r
    assert r["testbenches"] == ["sim/alu_tb.v", "sim/alu2_tb.v", "sim/top_tb.v"], r["testbenches"]
    assert r["testbenches_updated"] == ["sim/alu_tb.v"], "alu2_tb was asked (unchanged), top_tb only run"
    assert r["model_calls"] == 3 == len(m.calls), (r["model_calls"], len(m.calls))  # alu.v + 2 direct testbenches
    assert "top_tb.v: pass" in r["attempts"][0]["test_result"] and "inv_tb" not in r["attempts"][0]["test_result"]
    assert r["pending_diff"]["files"] == ["rtl/alu.v", "sim/alu_tb.v"], r["pending_diff"]
    assert "+      2'd2: y = a ^ b;" in r["diff"] and "+    op = 2; check(8'hCC);" in r["diff"], r["diff"]
    assert "| Round | Change | Test result | Root cause if failed |" in r["attempts_table"]
    assert "copilot_verify_" not in json.dumps(r), "no temp paths in the result"
    tb_prompt = m.calls[1]["messages"][1]["content"]
    assert "+      2'd2: y = a ^ b;" in tb_prompt and "Add XOR" in tb_prompt, "testbench edit sees the RTL diff"
    entry = module_modifier._load_pending()[r["pending_diff"]["diff_id"]]
    assert [Path(f["module_path"]) for f in entry["files"]] == [root / "rtl/alu.v", root / "sim/alu_tb.v"]
    print("verify_loop: passes in round 1; direct testbenches updated, indirect one run, 3 calls, one diff: OK")

    # --- the gate: "no" leaves the real files byte-identical ---
    before = sha_tree(root)
    with mock.patch.object(builtins, "input", return_value="n"), mock.patch("sys.stdout", new=open(os.devnull, "w")):
        assert cli.confirm_tool_call("apply_diff", {"diff_id": r["pending_diff"]["diff_id"]}) is False
    res = module_modifier.apply_diff(r["pending_diff"]["diff_id"])
    assert res["status"] == "declined" and sha_tree(root) == before, res
    print("verify_loop: a 'no' at the gate leaves the real files byte-identical: OK")

    # --- the gate: "yes" applies both files; a file changed after approval -> declined ---
    diff_id = r["pending_diff"]["diff_id"]
    diff_text, fp = module_modifier.preview_pending_diff(diff_id)
    assert diff_text.count("--- a/") == 2 and "+      2'd2: y = a ^ b;" in diff_text
    assert module_modifier.approve_pending_diff(diff_id, fp)
    (root / "sim/alu_tb.v").write_text(ALU_TB + "// edited meanwhile\n", encoding="utf-8")
    assert module_modifier.apply_diff(diff_id)["status"] == "declined"
    assert (root / "rtl/alu.v").read_text(encoding="utf-8") == ALU, "nothing written on a stale approval"
    (root / "sim/alu_tb.v").write_text(ALU_TB, encoding="utf-8")
    diff_text, fp = module_modifier.preview_pending_diff(diff_id)
    with mock.patch.object(builtins, "input", return_value="y"), mock.patch("sys.stdout", new=open(os.devnull, "w")):
        assert cli.confirm_tool_call("apply_diff", {"diff_id": diff_id}) is True
    assert module_modifier.apply_diff(diff_id)["status"] == "ok"
    assert (root / "rtl/alu.v").read_text(encoding="utf-8") == GOOD
    assert (root / "sim/alu_tb.v").read_text(encoding="utf-8") == ALU_TB_NEW
    print("verify_loop: 'yes' applies both files; stale approval declined, nothing written: OK")

    # --- all or nothing: second write fails -> first file restored ---
    root = project("p_atomic")
    a, b = root / "rtl/alu.v", root / "sim/alu_tb.v"
    diff_id = module_modifier.stage_pending([(str(a), GOOD), (str(b), ALU_TB_NEW)])
    _, fp = module_modifier.preview_pending_diff(diff_id)
    module_modifier.approve_pending_diff(diff_id, fp)
    os.chmod(b, stat.S_IREAD)
    try:
        res = module_modifier.apply_diff(diff_id)
    finally:
        os.chmod(b, stat.S_IREAD | stat.S_IWRITE)
    assert res["status"] == "error" and a.read_text(encoding="utf-8") == ALU, (res, "first file rolled back")
    assert diff_id in module_modifier._load_pending()
    # Codex review: a write that truncates its file and THEN fails is restored
    # too (exact original bytes); a failed restore is reported, never hidden.
    a.write_bytes(ALU.encode() + b"\r\n// crlf tail\r\n")
    a_bytes, b_bytes = a.read_bytes(), b.read_bytes()
    real_write_bytes = Path.write_bytes

    for restore_fails in (False, True):
        _, fp = module_modifier.preview_pending_diff(diff_id)
        module_modifier.approve_pending_diff(diff_id, fp)
        tb_writes = []

        def flaky_write(self, data):
            if self.name == "alu_tb.v":
                tb_writes.append(data)
                if len(tb_writes) == 1:  # the apply: truncates the file, then fails
                    real_write_bytes(self, data[:10])
                    raise OSError("disk full")
                if restore_fails:  # the rollback of that file
                    raise OSError("still full")
            return real_write_bytes(self, data)

        with mock.patch.object(Path, "write_bytes", flaky_write):
            res = module_modifier.apply_diff(diff_id)
        assert res["status"] == "error" and a.read_bytes() == a_bytes, res
        if restore_fails:
            assert res["files_not_restored"] == [str(b)] and "partly changed" in res["message"], res
        else:
            assert b.read_bytes() == b_bytes and "nothing applied" in res["message"], res
    print("verify_loop: multi-file apply is all-or-nothing (rollback incl. a truncated file; failed restore reported): OK")
    # Review round 3: the approved text is written once, in the file's own
    # line-ending style: no CR CR LF, no line breaks the diff didn't show.
    a.write_bytes(ALU.replace("\n", "\r\n").encode())       # a CRLF file...
    b.write_bytes(ALU_TB.encode())                          # ...and an LF file
    diff_id = module_modifier.stage_pending([(str(a), GOOD.replace("\n", "\r\n")), (str(b), ALU_TB_NEW)])
    diff_text, fp = module_modifier.preview_pending_diff(diff_id)
    assert "\r" not in diff_text, "CRLF proposals are normalised before they are shown"
    module_modifier.approve_pending_diff(diff_id, fp)
    assert module_modifier.apply_diff(diff_id)["status"] == "ok"
    assert a.read_bytes() == GOOD.replace("\n", "\r\n").encode() and b.read_bytes() == ALU_TB_NEW.encode()
    print("verify_loop: applied text written exactly once, keeping each file's line endings: OK")

    # --- 2. needs a debug round ---
    root = project("p2")
    m = Model({"alu.v": [OR_BUG, GOOD], "alu_tb.v": [ALU_TB_NEW]}, debug=[debug_reply()])
    r = loop(root, m)
    assert r["status"] == "passed" and len(r["attempts"]) == 2, r
    first, second = r["attempts"]
    assert "alu_tb.v: FAIL (1 failing, 2 passing)" in first["test_result"], first
    assert "alu.v:6 (high): op 2'd2 does not compute XOR." in first["root_cause"], first
    assert second["change"].startswith("alu.v:") and second["root_cause"] == "" and second["model_calls"] == 2
    assert r["model_calls"] == 5, r["model_calls"]
    dbg = [c for c in m.calls if c["messages"][0]["content"] == debugging._DEBUG_PROMPT]
    assert len(dbg) == 1 and dbg[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": True}}
    assert r["pending_diff"]["files"] == ["rtl/alu.v", "sim/alu_tb.v"] and "a | b" not in r["diff"]
    print("verify_loop: debug round finds the root cause, fixes the copy, passes in round 2 (5 calls): OK")

    # --- compile error -> one fix call, no reasoning call ---
    root = project("p_compile")
    m = Model({"alu.v": [SYNTAX_BUG, GOOD], "alu_tb.v": [ALU_TB_NEW]})
    r = loop(root, m)
    assert r["status"] == "passed" and len(r["attempts"]) == 2, r
    assert "does not compile" in r["attempts"][0]["root_cause"] and "alu.v" in r["attempts"][0]["root_cause"]
    assert "fix compile error" in r["attempts"][1]["change"] and r["model_calls"] == 4, r
    print("verify_loop: compile error fixed with one edit call: OK")

    # --- 3. hits the 3-round cap ---
    root = project("p3")
    m = Model({"alu.v": [OR_BUG, AND_BUG, SUB_BUG], "alu_tb.v": [ALU_TB_NEW]},
              debug=[debug_reply(), debug_reply(), debug_reply()])
    r = loop(root, m)
    assert r["status"] == "still_failing" and r["final_status"] == "still failing after 3 round(s)", r
    assert len(r["attempts"]) == 3 and all(a["root_cause"] for a in r["attempts"]), r["attempts"]
    assert r["model_calls"] == 3 + 2 + 2 + 1 and r["final_diagnosis_calls"] == 1, r["model_calls"]
    assert r["pending_diff"] and "still fail" in r["note"] and "a - b;" in r["diff"], r
    print("verify_loop: stops at the 3-round cap, last root cause reported, diff still staged (8 calls): OK")

    # --- Codex review: a testbench broken before the change ---
    BROKEN_ALU2 = FILES["sim/alu2_tb.v"].replace("check(8'h2C);", "check(8'h2C)")
    # (a) left alone by the model: not chased, but reported as unverified.
    root = project("p_unverified")
    (root / "sim/alu2_tb.v").write_text(BROKEN_ALU2, encoding="utf-8")
    r = loop(root, Model({"alu.v": [GOOD], "alu_tb.v": [ALU_TB_NEW]}))
    assert r["status"] == "passed" and r["verification"] == "partial", r
    assert r["unverified"] == {"sim/alu2_tb.v": "compile error"} and "don't verify it" in r["note"], r
    # (b) edited by the loop: it must pass too, so its compile error gets fixed.
    root = project("p_edited_broken")
    (root / "sim/alu2_tb.v").write_text(BROKEN_ALU2, encoding="utf-8")
    still_broken = BROKEN_ALU2.replace("    $finish;", "    op = 2; check(8'hCC)\n    $finish;")
    fixed = FILES["sim/alu2_tb.v"].replace("    $finish;", "    op = 2; check(8'hCC);\n    $finish;")
    r = loop(root, Model({"alu.v": [GOOD], "alu_tb.v": [ALU_TB_NEW], "alu2_tb.v": [still_broken, fixed]}))
    assert r["status"] == "passed" and len(r["attempts"]) == 2 and r["verification"] == "complete", r
    assert "alu2_tb.v does not compile" in r["attempts"][0]["root_cause"], r["attempts"]
    assert "sim/alu2_tb.v" in r["pending_diff"]["files"], r["pending_diff"]
    print("verify_loop: edited testbenches must pass; untouched pre-broken ones reported as unverified: OK")

    # --- Codex review, round 2 ---
    from src.copilot.tools.hdl_safety import new_risky_features  # noqa: E402
    # (1) an edit that ADDS file/process access is never simulated.
    root = project("p_risky")
    evil = GOOD.replace("  always @(*) begin", '  integer f;\n  initial f = $fopen("C:/Users/victim/notes.txt", "w");\n'
                        "  always @(*) begin")
    m = Model({"alu.v": [evil]})
    r = loop(root, m)
    assert r["status"] == "blocked" and "$fopen" in r["message"] and r["pending_diff"] is None, r
    assert r["model_calls"] == 1, "stopped before the testbench edits"
    old_tb = 'initial $readmemh("prog.hex", mem);\n`include "defs.vh"\n'
    assert new_risky_features(old_tb, old_tb + "// moved nothing\n") == [], "the user's own I/O is not counted"
    assert new_risky_features(old_tb, old_tb.replace("prog.hex", "/etc/passwd")) == ['$readmemh("/etc/passwd", mem)']
    assert new_risky_features("", "`define include x\n") and new_risky_features("", "$`T(1);")
    assert new_risky_features("", "assert property ($past(x) |-> $rose(y));") == [], "pure SV functions allowed"
    # Full review: uncommenting an existing call is new access.
    assert new_risky_features('// f = $fopen("o.txt", "w");\n', 'f = $fopen("o.txt", "w");\n') == ['$fopen("o.txt","w")']
    # Review round 2: the whole call is the key, so a new mode or a built-up path is new access.
    assert new_risky_features('f = $fopen("o.txt", "r");', 'f = $fopen("o.txt", "w");') == ['$fopen("o.txt","w")']
    assert new_risky_features('$readmemh(MEM, m);', '$readmemh({"C:", "/x"}, m);') == ['$readmemh({"C:","/x"}, m)']
    # The simulation guard: compares preprocessed inputs with the originals.
    g = work / "guard"
    for sub in ("project", "original"):
        (g / sub).mkdir(parents=True)
        (g / sub / "defs.vh").write_text('`define LOG(p) $fopen(p, "w")\n', encoding="utf-8")
        (g / sub / "m.v").write_text('`include "defs.vh"\nmodule m; endmodule\n', encoding="utf-8")
        (g / sub / "r.v").write_text('module r #(parameter MEM = "ops.hex"); reg [7:0] k [0:1];\n'
                                     '  initial $readmemh(MEM, k); endmodule\n', encoding="utf-8")
        (g / sub / "w.v").write_text('module w; reg [8*8:1] n = "o.txt"; integer f;\n'
                                     '  initial f = $fopen(n, "w"); endmodule\n', encoding="utf-8")
    gc = vl_mod._Copy((g / "project").resolve())
    guard = vl_mod._sim_guard(gc, (g / "original").resolve(), None)
    proj_files = [(g / "project" / n).resolve() for n in ("m.v", "r.v", "w.v")]
    assert guard(proj_files) is None, "nothing edited: the user's own code runs"
    # (a) a macro hides $fopen from a raw-text check; the preprocessed text shows it.
    gc.write(g / "project" / "m.v", '`include "defs.vh"\nmodule m; integer f; initial f = `LOG("C:/x.txt"); endmodule\n')
    assert vl_mod.new_risky_features('`include "defs.vh"\n', (g / "project/m.v").read_text()) == [], "raw check misses it"
    reason = guard([(g / "project" / "m.v").resolve()])
    assert reason and '$fopen("C:/x.txt","w")' in reason, reason
    # (b) an edited design that writes through a non-literal path is not run.
    gc.edited.clear()
    gc.write(g / "project" / "w.v", (g / "original/w.v").read_text() + "// touched\n")
    assert "non-literal path" in guard([(g / "project" / "w.v").resolve()])
    # (c) review round 2: any non-literal path (reads too) in a design the loop
    # changed is not run: a parameter or concatenation could steer it.
    gc.edited.clear()
    assert guard([(g / "project" / "r.v").resolve()]) is None, "nothing edited yet"
    gc.write(g / "project" / "r.v", (g / "original/r.v").read_text() + "// touched\n")
    reason = guard([(g / "project" / "r.v").resolve()])
    assert reason and "non-literal path" in reason and "$readmemh(MEM, k)" in reason, reason
    # End to end: a blocked simulation stops the loop and stages nothing.
    root = project("p_macro")
    (root / "rtl/defs.vh").write_text('`define LOG(p) $fopen(p, "w")\n', encoding="utf-8")
    sneaky = GOOD.replace("module alu(", '`include "defs.vh"\nmodule alu(').replace(
        "  always @(*) begin", '  integer f;\n  initial f = `LOG("C:/Users/victim/x.txt");\n  always @(*) begin')
    (root / "rtl/alu.v").write_text(ALU.replace("module alu(", '`include "defs.vh"\nmodule alu('), encoding="utf-8")
    r = loop(root, Model({"alu.v": [sneaky]}))
    assert r["status"] == "blocked" and "$fopen" in r["message"] and r["pending_diff"] is None, r
    # (2) Vivado mem_init_files outside project_dir are found in the copy too.
    outer = work / "p_vivado"
    for rel, text in FILES.items():
        (outer / "demo.srcs" / rel).parent.mkdir(parents=True, exist_ok=True)
        (outer / "demo.srcs" / rel).write_text(text, encoding="utf-8")
    (outer / "demo.xpr").write_text("<Project/>\n", encoding="utf-8")
    (outer / "demo.ip_user_files" / "mem_init_files").mkdir(parents=True)
    (outer / "demo.ip_user_files" / "mem_init_files" / "ops.hex").write_text("00\n01\n02\n", encoding="utf-8")
    mem_tb = ALU_TB_NEW.replace("module alu_tb;", "module alu_tb;\n  reg [7:0] ops [0:2];").replace(
        "    a = 8'hF0;", '    $readmemh("ops.hex", ops);\n    a = 8\'hF0;').replace("op = 2;", "op = ops[2];")
    (outer / "demo.srcs/sim/alu_tb.v").write_text(mem_tb, encoding="utf-8")
    r = loop(outer / "demo.srcs", Model({"alu.v": [GOOD]}))
    assert r["status"] == "passed" and r["attempts"][0]["test_result"].startswith("alu_tb.v: pass (3 checks)"), r
    # (3) any input changing during the loop (not only edited files) -> nothing staged.
    root = project("p_stale")
    m = Model({"alu.v": [GOOD], "alu_tb.v": [ALU_TB_NEW]})
    real_create = m.create

    def create_and_touch(**kw):
        (root / "rtl/top.v").write_text(FILES["rtl/top.v"] + "// edited meanwhile\n", encoding="utf-8")
        return real_create(**kw)

    m.create = create_and_touch
    client = m.client()
    with mock.patch.object(module_modifier, "get_client", return_value=client), \
         mock.patch.object(debugging, "get_client", return_value=client):
        r = vl_mod.verify_loop(goal="Add XOR at op 2'd2.", module_path=str(root / "rtl/alu.v"),
                               tb_path=str(root / "sim/alu_tb.v"))
    assert r["status"] == "error" and "rtl/top.v" in r["message"] and r["pending_diff"] is None, r
    print("verify_loop: risky edits never simulated, Vivado mem_init found, stale inputs stage nothing: OK")

    # --- review round 2: lint decides too ---
    def fake_lint(path, compile_check):
        if "original" in Path(path).parts:
            return {"warnings": 0, "errors": ["%Error: alu.v:1:1: Cannot find module 'x'"]}  # pre-existing
        text = Path(path).read_text(encoding="utf-8")
        errors = ["%Error: alu.v:9:1: Cannot find module 'x'"]  # same error, moved: not new
        if "BADLINT" in text:
            errors.append("%Error: alu.v:7:5: Signal 'q' is not declared")
        return {"warnings": 0, "errors": errors}

    root = project("p_lint")
    m = Model({"alu.v": [GOOD.replace("endmodule", "// BADLINT\nendmodule"), GOOD], "alu_tb.v": [ALU_TB_NEW]})
    with mock.patch.object(vl_mod, "_lint", side_effect=fake_lint):
        r = loop(root, m)
    assert r["status"] == "passed" and len(r["attempts"]) == 2, r
    assert "new lint error(s) in alu.v: %Error: alu.v:7:5: Signal 'q' is not declared" in r["attempts"][0]["root_cause"]
    assert "fix lint error" in r["attempts"][1]["change"] and r["model_calls"] == 4 and "BADLINT" not in r["diff"], r
    # A lint run the guard refuses stops the loop like a blocked simulation.
    root = project("p_lint_blocked")
    with mock.patch.object(vl_mod, "_lint", side_effect=lambda path, cc: {"blocked": "nope"}
                           if "original" not in Path(path).parts else {"warnings": 0, "errors": []}):
        r = loop(root, Model({"alu.v": [GOOD], "alu_tb.v": [ALU_TB_NEW]}))
    assert r["status"] == "blocked" and "lint alu.v: nope" in r["message"] and r["pending_diff"] is None, r
    print("verify_loop: new lint errors are fixed before 'passed'; old/moved ones ignored; blocked lint stops: OK")

    # --- review round 3 ---
    # Spaces inside a path literal are significant.
    assert new_risky_features('$fopen("a  b.txt");', '$fopen("a b.txt");') == ['$fopen("a b.txt")']
    # Enabling an existing branch that writes outside the run folder: refused,
    # and the user's own testbench that does so is never run by the loop.
    from src.copilot.tools.hdl_safety import access_features, escaping_access  # noqa: E402
    assert escaping_access(access_features('if (1) f = $fopen("C:/Users/me/notes.txt", "w"); $system("x");')) == \
        ['$fopen("C:/Users/me/notes.txt","w")', '$system("x")']
    assert escaping_access(access_features('f = $fopen("run.log", "w"); $readmemh("C:/p.hex", m);')) == []
    root = project("p_escape")
    (root / "sim/top_tb.v").write_text(FILES["sim/top_tb.v"].replace(
        "  initial begin", '  integer f;\n  initial begin\n    if (0) f = $fopen("C:/Users/me/notes.txt", "w");'),
        encoding="utf-8")
    r = loop(root, Model({"alu.v": [GOOD], "alu_tb.v": [ALU_TB_NEW]}))
    assert r["status"] == "passed" and "sim/top_tb.v" not in r["testbenches"], r
    assert "writes outside the simulation folder" in r["testbenches_not_run"]["sim/top_tb.v"], r
    assert r["verification"] == "partial", r
    # Testbenches the loop edits always run, whatever the testbench cap.
    root = project("p_cap")
    with mock.patch.object(vl_mod, "_MAX_TESTBENCHES", 1):
        r = loop(root, Model({"alu.v": [GOOD], "alu_tb.v": [ALU_TB_NEW]}))
    assert r["testbenches"] == ["sim/alu_tb.v", "sim/alu2_tb.v"], r["testbenches"]
    assert r["testbenches_not_run"] == {"sim/top_tb.v": "over the testbench limit"}, r
    # Lint that can't run on an edited file: no "complete" verification.
    root = project("p_nolint")
    with mock.patch.object(vl_mod, "_lint", side_effect=lambda path, cc: {"unavailable": "unavailable"}):
        r = loop(root, Model({"alu.v": [GOOD], "alu_tb.v": [ALU_TB_NEW]}))
    assert r["status"] == "passed" and r["verification"] == "partial" and r["lint_not_run"] == {"rtl/alu.v": "unavailable"}
    print("verify_loop: path spaces kept, out-of-folder writes never run, edited tbs always run, lint gaps reported: OK")

    # --- no change from the model -> nothing staged ---
    root = project("p_none")
    r = loop(root, Model({}))
    assert r["status"] == "no_change" and r["pending_diff"] is None and r["model_calls"] == 3, r

    # --- an error mid-loop still deletes the copy ---
    root = project("p_err")
    with mock.patch.object(vl_mod, "_affected_testbenches", side_effect=RuntimeError("boom")):
        r = loop(root, Model({"alu.v": [GOOD]}))
    assert r["status"] == "error" and "boom" in r["message"] and r["pending_diff"] is None, r

    # --- writes outside the copy are refused ---
    copy = vl_mod._Copy(work / "p_err")
    try:
        copy.write(work / "outside.v", "x")
        raise AssertionError("wrote outside the copy")
    except RuntimeError:
        pass
    assert not (work / "outside.v").exists()
    # Bad inputs stop before any copy is made.
    assert vl_mod.verify_loop(goal="", module_path="x.v", tb_path="y.v")["status"] == "error"
    assert vl_mod.verify_loop(goal="g", module_path=str(root / "rtl/alu.v"), tb_path=str(root / "sim/alu_tb.v"),
                              project_dir=str(root / "rtl"))["status"] == "error", "tb outside project_dir"
    print("verify_loop: no-change, mid-loop error and bad inputs clean up and stage nothing: OK")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\nVERIFY LOOP TESTS PASSED")
