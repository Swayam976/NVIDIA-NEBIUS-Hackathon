"""testbench_runner on multi-file designs and spec_drafting_assistant's
wiring + name check (TASKS.md #14). No network: the model is mocked.
Simulation cases need iverilog/vvp on PATH and are skipped otherwise.
"""

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot.config import settings  # noqa: E402
from src.copilot.tools import docs, verification  # noqa: E402
from src.copilot.tools.rtl_files import design_files, instance_connections  # noqa: E402

TB = """`timescale 1ns/1ps
module top_tb;
  reg [7:0] prog [0:3];
  wire [7:0] y;
  top dut (.a(prog[0]), .b(prog[1]), .y(y));
  initial begin
    $readmemh("{mem}", prog);
    #1;
    if (y === 8'h0f) $display("PASS y=%h", y); else $display("FAIL y=%h", y);
    $finish;
  end
endmodule
"""

tmp = Path(tempfile.mkdtemp(prefix="copilot_multi_"))
try:
    src = tmp / "proj" / "src"
    (src / "unrelated").mkdir(parents=True)
    (tmp / "proj" / "sim" / "tb").mkdir(parents=True)
    (tmp / "proj" / "sim" / "data").mkdir(parents=True)
    (src / "top.v").write_text(
        "module top(input [7:0] a, input [7:0] b, output [7:0] y);\n"
        "  wire [7:0] t;\n"
        "  sub_a #(.W(8)) ua (.x(a), .y(t));\n"
        "  sub_b ub (.p(t), .q(b), .r(y));\n"
        "endmodule\n", encoding="utf-8")
    (src / "sub_a.v").write_text(
        "module sub_a #(parameter W = 8)(input [W-1:0] x, output [W-1:0] y);\n  assign y = x;\nendmodule\n",
        encoding="utf-8")
    (src / "sub_b.v").write_text(
        "module sub_b(input [7:0] p, input [7:0] q, output [7:0] r);\n  assign r = p & q;\nendmodule\n",
        encoding="utf-8")
    # Never instantiated: must not be compiled (it would not even elaborate).
    (src / "unrelated" / "other_top.v").write_text(
        "module other_top;\n  missing_module m (.x(1'b0));\nendmodule\n", encoding="utf-8")
    tb_dir = tmp / "proj" / "sim" / "tb"
    (tb_dir / "top_tb.v").write_text(TB.replace("{mem}", "prog.mem"), encoding="utf-8")
    (tmp / "proj" / "sim" / "data" / "prog.mem").write_text("1f\n0f\n00\n00\n", encoding="utf-8")

    # --- dependency closure (no simulator needed) ---
    tb = (tb_dir / "top_tb.v").resolve()
    search = sorted(p.resolve() for p in src.rglob("*.v"))
    tops, files, dups = design_files(tb, search)
    assert tops == ["top_tb"] and dups == {}, (tops, dups)
    assert sorted(f.name for f in files) == ["sub_a.v", "sub_b.v", "top.v", "top_tb.v"], files
    # Strings are not code (Codex review, task 14): a $display text that looks
    # like an instantiation pulls nothing in, and a "//" inside a string does
    # not hide a real instantiation after it on the same line.
    top_v = (src / "top.v").read_text(encoding="utf-8")
    (src / "top.v").write_text(
        "module top(input [7:0] a, input [7:0] b, output [7:0] y);\n"
        "  wire [7:0] t;\n"
        '  initial $display("other_top fake (");\n'
        "  sub_a #(.W(8)) ua (.x(a), .y(t));\n"
        '  initial $display("//"); sub_b ub (.p(t), .q(b), .r(y));\n'
        "endmodule\n", encoding="utf-8")
    tops, files, dups = design_files(tb, search)
    assert sorted(f.name for f in files) == ["sub_a.v", "sub_b.v", "top.v", "top_tb.v"], files
    (src / "top.v").write_text(top_v, encoding="utf-8")
    print("design_files: follows instantiations (incl. #(...)), skips unrelated modules and strings: OK")

    # --- instance connections for spec drafting ---
    wiring, n = instance_connections(tmp / "proj", hint="sub_b")
    assert n == 2 and wiring.splitlines()[0] == "top: sub_b ub (.p(t), .q(b), .r(y))", wiring
    assert "top: sub_a ua (.x(a), .y(t))" in wiring, "parameter override list skipped correctly"
    print("instance_connections: port connections, parameter lists skipped, hint-relevant first: OK")

    # --- spec drafting: wiring in the prompt, invented names flagged ---
    def fake(draft, sink):
        def create(**kw):
            sink.append(kw)
            msg = types.SimpleNamespace(content=draft)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    calls = []
    draft = ("`top` feeds `a` through `sub_a` (`ua`) into `sub_b`, whose `r` drives `y`. "
             "The `bypass_sel` mux and the carry_chain_out net are TBD. Width is `8'hff`; see `top.v`.")
    with mock.patch.dict(settings.project_repo_paths, {"riscv-core": str(tmp / "proj")}), \
         mock.patch.object(docs, "get_client", return_value=fake(draft, calls)):
        r = docs.spec_drafting_assistant("riscv-core", "datapath")
    prompt = calls[0]["messages"][1]["content"]
    assert "RTL instance connections" in prompt and "top: sub_b ub (.p(t), .q(b), .r(y))" in prompt, prompt
    assert r["unverified_names"] == ["bypass_sel", "carry_chain_out"], r
    assert "bypass_sel" in r["note"] and r["wiring_lines_given"] == 2
    with mock.patch.dict(settings.project_repo_paths, {"riscv-core": str(tmp / "nowhere")}), \
         mock.patch.object(docs, "get_client", return_value=fake(draft, [])):
        r = docs.spec_drafting_assistant("riscv-core", "datapath")
    assert r["grounded_on_modules"] == [] and "could not be checked" in r["note"] and "top" in r["unverified_names"]
    with mock.patch.object(docs, "get_client", return_value=fake("   ", [])):
        assert docs.spec_drafting_assistant("riscv-core", "x")["status"] == "error", "empty draft must not pass"
    names = docs._draft_names("`2'b10` `always @(posedge clk)` `forward_unit.v` `TBD` `a` and load_use_stall")
    assert names == ["a", "clk", "forward_unit", "load_use_stall"], names  # single letters checked too
    # Case-sensitive (Verilog), words only inside RTL strings don't count, and
    # every name is checked even past the 25 shown (Codex review, task 14).
    (src / "sub_b.v").write_text(
        "module sub_b(input [7:0] p, input [7:0] q, output [7:0] r);\n"
        '  assign r = p & q;\n  initial $display("ghost_signal ready");\nendmodule\n', encoding="utf-8")
    many = " ".join(f"`n{i:02d}_ok`" for i in range(30))
    for i in range(30):
        (src / f"pad{i:02d}.v").write_text(f"module pad{i:02d}; wire n{i:02d}_ok; endmodule\n", encoding="utf-8")
    draft2 = f"`Sub_A` feeds `sub_b`; `ghost_signal` is set; {many}; `zz_invented` too; `y` is real, `z` is not."
    with mock.patch.dict(settings.project_repo_paths, {"riscv-core": str(tmp / "proj")}), \
         mock.patch.object(docs, "get_client", return_value=fake(draft2, [])):
        r = docs.spec_drafting_assistant("riscv-core", "datapath")
    assert r["unverified_names"] == ["Sub_A", "ghost_signal", "z", "zz_invented"] and r["unverified_count"] == 4, r
    assert r["names_checked"] == 36, r
    for i in range(30):
        (src / f"pad{i:02d}.v").unlink()
    print("spec drafting: wiring in prompt, invented names flagged, no-RTL + empty draft handled: OK")

    # --- safety of the program-file search ---
    home = Path.home()
    assert not verification._safe_data_root(home) and not verification._safe_data_root(Path(home.anchor))
    assert verification._safe_data_root(tmp)
    print("program-file search never scans the home folder or a drive root: OK")

    schema = next(s for s in verification.SCHEMAS if s["function"]["name"] == "testbench_runner")
    assert schema["function"]["parameters"]["required"] == ["tb_path"]
    assert verification.testbench_runner(tb_path=str(tb))["status"] in ("error", "unavailable")
    assert verification.testbench_runner(tb_path=str(tmp / "no_tb.v"), rtl_dir=str(src))["status"] in ("error", "unavailable")

    if not (shutil.which("iverilog") and shutil.which("vvp")):
        print("testbench_runner multi-file: SKIPPED (iverilog/vvp not on PATH)")
    else:
        r = verification.testbench_runner(tb_path=str(tb), rtl_dir=str(src))
        assert r["status"] == "pass" and r["data_files"] == ["prog.mem"] and r["top"] == ["top_tb"], r
        assert sorted(r["compiled_files"]) == ["sub_a.v", "sub_b.v", "top.v", "top_tb.v"], r["compiled_files"]
        # Relative paths work although compile and run happen in a temp dir.
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            r = verification.testbench_runner(tb_path="proj/sim/tb/top_tb.v", rtl_dir="proj/src")
        finally:
            os.chdir(cwd)
        assert r["status"] == "pass", r
        print("testbench_runner: multi-file design via rtl_dir, program file from a sibling folder, relative paths: OK")

        (tb_dir / "absent_tb.v").write_text(TB.replace("top_tb", "absent_tb").replace("{mem}", "absent.mem"), encoding="utf-8")
        r = verification.testbench_runner(tb_path=str(tb_dir / "absent_tb.v"), rtl_dir=str(src))
        assert r["status"] == "missing_data_file" and r["missing_data_files"] == ["absent.mem"], r
        assert r["headline"].startswith("The simulation could not open absent.mem"), r["headline"]
        (tb_dir / "absent_tb.v").unlink()
        print("testbench_runner: missing program file reported as such, not as an RTL failure: OK")

        # A macro-hidden instantiation the closure can't see: iverilog names the
        # unknown module and the runner adds its file and recompiles.
        (src / "top.v").write_text(
            "`define SUB_B sub_b\nmodule top(input [7:0] a, input [7:0] b, output [7:0] y);\n"
            "  wire [7:0] t;\n  sub_a #(.W(8)) ua (.x(a), .y(t));\n  `SUB_B ub (.p(t), .q(b), .r(y));\nendmodule\n",
            encoding="utf-8")
        r = verification.testbench_runner(tb_path=str(tb), rtl_dir=str(src))
        assert r["status"] == "pass" and "sub_b.v" in r["compiled_files"], r
        # A module added by that retry can `include a header next to it (Codex review).
        (src / "inc").mkdir()
        (src / "inc" / "and_op.vh").write_text("`define AND_OP(x, y) ((x) & (y))\n", encoding="utf-8")
        (src / "sub_b.v").rename(src / "inc" / "sub_b.v")
        (src / "inc" / "sub_b.v").write_text(
            '`include "and_op.vh"\nmodule sub_b(input [7:0] p, input [7:0] q, output [7:0] r);\n'
            "  assign r = `AND_OP(p, q);\nendmodule\n", encoding="utf-8")
        r = verification.testbench_runner(tb_path=str(tb), rtl_dir=str(src))
        assert r["status"] == "pass" and "inc/sub_b.v" in r["compiled_files"], r
        print("testbench_runner: module iverilog reports as unknown is added and recompiled (with its includes): OK")

        # The same fallback must not guess between two definitions (Codex review).
        (src / "unrelated" / "sub_b.v").write_text("module sub_b(input [7:0] p, q, output [7:0] r); endmodule\n",
                                                   encoding="utf-8")
        r = verification.testbench_runner(tb_path=str(tb), rtl_dir=str(src))
        assert r["status"] == "ambiguous_design" and set(r["duplicates"]["sub_b"]) == {"inc/sub_b.v", "unrelated/sub_b.v"}, r
        (src / "unrelated" / "sub_b.v").unlink()

        # compile_check sees the exact compiler inputs, tb first, before EVERY
        # attempt (incl. the retry that adds sub_b.v); a reason blocks the run.
        seen = []
        def check(files):
            seen.append([f.name for f in files])
            return "blocked by test" if len(seen) == 2 else None
        r = verification.testbench_runner(tb_path=str(tb), rtl_dir=str(src), compile_check=check)
        assert r == {"status": "blocked", "message": "blocked by test"}, r
        assert seen[0][0] == "top_tb.v" and "sub_b.v" not in seen[0] and "sub_b.v" in seen[1], seen
        print("testbench_runner: fallback never guesses; compile_check gets exact inputs per attempt: OK")

        (src / "unrelated" / "sub_a.v").write_text("module sub_a(input x, output y); endmodule\n", encoding="utf-8")
        r = verification.testbench_runner(tb_path=str(tb), rtl_dir=str(src))
        assert r["status"] == "ambiguous_design" and set(r["duplicates"]["sub_a"]) == {"sub_a.v", "unrelated/sub_a.v"}, r
        (src / "unrelated" / "sub_a.v").unlink()
        print("testbench_runner: a module defined twice is reported, not guessed: OK")

        # Same-named program files (Codex review, round 3): the copy beside the
        # testbench wins and a DIFFERENT copy elsewhere is reported; an
        # identical copy is not a conflict.
        (tb_dir / "prog.mem").write_text("ff\n0f\n00\n00\n", encoding="utf-8")  # different bytes, same result
        (tmp / "proj" / "sim" / "copy").mkdir()
        shutil.copy2(tb_dir / "prog.mem", tmp / "proj" / "sim" / "copy" / "prog.mem")  # identical
        r = verification.testbench_runner(tb_path=str(tb), rtl_dir=str(src))
        assert r["status"] == "pass", r
        assert r["data_file_conflicts"] == {"prog.mem": {"used": "tb/prog.mem", "not_used": ["data/prog.mem"]}}, r
        (tb_dir / "prog.mem").unlink()
        shutil.rmtree(tmp / "proj" / "sim" / "copy")
        r = verification.testbench_runner(tb_path=str(tb), rtl_dir=str(src))
        assert "data_file_conflicts" not in r, r
        print("testbench_runner: same-named program files: closest used, different copies reported: OK")

        # SystemVerilog sources compile with -g2012 (always_comb, logic).
        (src / "inv.sv").write_text(
            "module inv(input logic [7:0] i, output logic [7:0] o);\n  always_comb o = ~i;\nendmodule\n",
            encoding="utf-8")
        (tb_dir / "inv_tb.v").write_text(
            "module inv_tb; reg [7:0] i = 8'h0f; wire [7:0] o; inv u (.i(i), .o(o));\n"
            '  initial begin #1; if (o === 8\'hf0) $display("PASS"); else $display("FAIL o=%h", o); $finish; end\n'
            "endmodule\n", encoding="utf-8")
        r = verification.testbench_runner(tb_path=str(tb_dir / "inv_tb.v"), rtl_dir=str(src))
        assert r["status"] == "pass" and "inv.sv" in r["compiled_files"], r
        (tb_dir / "inv_tb.v").unlink()
        print("testbench_runner: .sv sources compile in SystemVerilog mode: OK")

        # PASS printed, then $fatal: a non-zero simulator exit is never a pass.
        (tb_dir / "fatal_tb.v").write_text(
            'module fatal_tb; initial begin $display("PASS early"); $fatal(1, "boom"); end endmodule\n',
            encoding="utf-8")
        r = verification.testbench_runner(tb_path=str(tb_dir / "fatal_tb.v"), rtl_dir=str(src))
        assert r["status"] != "pass" and r["exit_code"] not in (0, None), r
        (tb_dir / "fatal_tb.v").unlink()
        print("testbench_runner: PASS followed by $fatal is reported as a failure: OK")

        (tb_dir / "hang_tb.v").write_text("module hang_tb; reg clk = 0; top dut(); always #5 clk = ~clk; endmodule\n",
                                          encoding="utf-8")
        r = verification.testbench_runner(tb_path=str(tb_dir / "hang_tb.v"), rtl_dir=str(src), timeout_s=3)
        assert r["status"] == "timeout" and "did not finish" in r["message"], r
        print("testbench_runner: a simulation without $finish times out cleanly: OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\nMULTI-FILE TESTS PASSED")
