"""Web-demo guardrails (src/copilot/sandbox.py): path confinement, project
slugs, schema-filtered args, the HDL source check (incl. the include and
token-paste tricks that bypass a naive scan), and pending-diff filtering.

HDL-check cases need iverilog on PATH; they are skipped otherwise.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot import memory, sandbox  # noqa: E402
from src.copilot.config import MEMORY_DIR  # noqa: E402
from src.copilot.tools import module_modifier  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
ws = sandbox.create_workspace(MEMORY_DIR, REPO / "demo" / "sample")
outside = Path(tempfile.mkdtemp(prefix="copilot_outside_"))
try:
    # --- workspace contents ---
    files = ws.files()
    assert "rtl/alu.v" in files and "rtl/alu_tb.v" in files and "memory/sample-alu.md" in files, files
    print("workspace: memory + sample RTL copied:", files)

    # --- path confinement ---
    assert ws.resolve("rtl/alu.v") == (ws.root / "rtl" / "alu.v").resolve()
    for bad in ["../x.v", "rtl/../../x.v", str(outside / "x.v"), "/etc/passwd", ".pending_diffs.json", "rtl/.hidden.v"]:
        try:
            ws.resolve(bad)
        except sandbox.SandboxError:
            continue
        raise AssertionError(f"escaped workspace: {bad}")
    print("resolve(): traversal, absolute, and hidden paths rejected: OK")

    schemas, impls = sandbox.guarded_tools(ws)
    names = {s["function"]["name"] for s in schemas}
    assert "apply_diff" not in names and "apply_diff" not in impls, "model must never get apply_diff in the demo"
    assert len(names) == 21
    print("guarded_tools: apply_diff withheld from the model, 21 tools offered: OK")

    # --- guarded tool calls ---
    (outside / "secret.v").write_text("module s; endmodule\n", encoding="utf-8")
    r = impls["lint_checker"](file_path=str(outside / "secret.v"))
    assert r["status"] == "error" and "outside" in r["message"], r
    r = impls["project_state_tracker"](project="../../README")
    assert r["status"] == "error" and "Unknown project" in r["message"], r
    r = impls["daily_brief"](projects=["sample-alu", "../x"])
    assert r["status"] == "error", r
    with sandbox.activate(ws):
        r = impls["project_state_tracker"](project="sample-alu")
    assert r["status"] == "ok" and "XOR" in r["current_status"], r
    print("guarded calls: outside path / bad project slug refused, valid call OK")

    # Empty / "rv32i" spec_path selects the built-in RV32I list instead of being
    # resolved as a path (model call stubbed: no network in this test).
    import types  # noqa: E402
    from unittest import mock  # noqa: E402
    from src.copilot.tools import docs  # noqa: E402

    def _no_answer(**kwargs):
        msg = types.SimpleNamespace(content='{"instructions": []}')
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    stub = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=_no_answer)))
    for spec in ("", "rv32i"):
        with mock.patch.object(docs, "get_client", return_value=stub):
            r = impls["isa_spec_cross_referencer"](rtl_dir="rtl", spec_path=spec)
        assert r["status"] == "ok" and r["mode"] == "rv32i" and len(r["unclear"]) == 40, r
    r = impls["isa_spec_cross_referencer"](rtl_dir="rtl", spec_path="../../etc/passwd")
    assert r["status"] == "error" and "outside" in r["message"], r
    # Only the cross-referencer treats "rv32i" as a keyword; for the auditor it
    # is a path and must stay inside the workspace (not the server's cwd).
    seen = {}
    real = sandbox.TOOL_IMPLS["testbench_auditor"]
    sandbox.TOOL_IMPLS["testbench_auditor"] = lambda **kw: seen.update(kw) or {"status": "ok"}
    try:
        _, impls_a = sandbox.guarded_tools(ws)
        impls_a["testbench_auditor"](tb_path="rtl/alu_tb.v", rtl_dir="rtl", spec_path="rv32i")
        assert Path(seen["spec_path"]) == ws.root.resolve() / "rv32i", seen
        seen.clear()
        impls_a["testbench_auditor"](tb_path="rtl/alu_tb.v", rtl_dir="rtl", spec_path="")
        assert seen["spec_path"] == "", seen
    finally:
        sandbox.TOOL_IMPLS["testbench_auditor"] = real
    print("guarded isa_spec_cross_referencer: RV32I mode in the workspace, spec paths still confined: OK")

    # generate_rtl: repo_root, target_path and every connect_to path stay in the workspace.
    for kwargs in ({"repo_root": str(outside), "target_path": "x.v"},
                   {"repo_root": ".", "target_path": str(outside / "x.v")},
                   {"repo_root": ".", "target_path": "rtl/x.v", "connect_to": ["../../etc/passwd"]},
                   {"repo_root": ".", "target_path": "rtl/x.v", "connect_to": "rtl/alu.v"}):
        r = impls["generate_rtl"](requirements="a register", module_name="x", **kwargs)
        assert r["status"] == "error", (kwargs, r)
    print("guarded generate_rtl: repo root, target and connect_to paths confined: OK")
    for kwargs in ({"module_path": str(outside / "x.v"), "repo_root": "."},
                   {"module_path": "rtl/alu.v", "repo_root": ".", "target_path": "../../x_tb.v"},
                   {"module_path": "rtl/alu.v", "repo_root": ".", "target_path": "rtl/alu_tb.v"}):  # exists
        r = impls["generate_testbench"](**kwargs)
        assert r["status"] == "error", (kwargs, r)
    print("guarded generate_testbench: module and target confined, no overwrite: OK")
    for kwargs in ({"rtl_path": str(outside / "x.v"), "tb_path": "rtl/x_tb.v"},
                   {"rtl_path": "rtl/x.v", "tb_path": "../../x_tb.v"},
                   {"rtl_path": "rtl/alu.v", "tb_path": "rtl/x_tb.v"}):  # exists
        r = impls["create_module"](requirements="a register", module_name="x", repo_root=".", **kwargs)
        assert r["status"] == "error", (kwargs, r)
    print("guarded create_module: both new paths confined, no overwrite: OK")

    # Smuggled kwargs (not in the tool schema) are dropped, not passed through.
    seen = {}
    real = sandbox.TOOL_IMPLS["hazard_sanity_checker"]
    sandbox.TOOL_IMPLS["hazard_sanity_checker"] = lambda **kw: seen.update(kw) or {"status": "ok"}
    try:
        _, impls2 = sandbox.guarded_tools(ws)
        impls2["hazard_sanity_checker"](diff_text="+x", timeout_s=99999, evil=1)
    finally:
        sandbox.TOOL_IMPLS["hazard_sanity_checker"] = real
    assert seen == {"diff_text": "+x"}, seen
    print("guarded calls: args outside the schema are dropped: OK")

    # --- pending diffs that point outside the workspace are dropped unpreviewed ---
    with sandbox.activate(ws):
        module_modifier._save_pending({
            "good": {"module_path": str(ws.root / "rtl" / "alu.v"), "new_content": "x"},
            "evil": {"module_path": "/proc/self/environ", "new_content": "x"},
            # A multi-file change (verify_loop) is dropped if ANY file is outside.
            "multi_good": {"files": [{"module_path": str(ws.root / "rtl" / "alu.v"), "new_content": "x"},
                                     {"module_path": str(ws.root / "rtl" / "alu_tb.v"), "new_content": "y"}]},
            "multi_evil": {"files": [{"module_path": str(ws.root / "rtl" / "alu.v"), "new_content": "x"},
                                     {"module_path": "/etc/passwd", "new_content": "y"}]},
            "empty": {"files": []},
        })
        assert sandbox.pending_diff_ids(ws) == ["good", "multi_good"]
        assert not {"evil", "multi_evil", "empty"} & module_modifier._load_pending().keys()
        module_modifier._save_pending({})
    assert memory.MEMORY_DIR == MEMORY_DIR, "activate() must restore the globals"
    # Diff panel: every file's header loses the server path; hunk bodies are
    # untouched, even a removed/added pair that looks like a header.
    root = str(ws.root)
    two_files = (f"--- a/{root}/rtl/alu.v\n+++ b/{root}/rtl/alu.v\n@@ -1,2 +1,2 @@\n"
                 f"--- {root} looks like a header\n+++ {root} too\n@@ -1 +1 @@ not a hunk\n"
                 f"--- a/{root}/rtl/alu_tb.v\n+++ b/{root}/rtl/alu_tb.v\n@@ -3 +3 @@\n-x\n+y\n")
    shown = sandbox.display_diff(ws, two_files).splitlines()
    assert shown[0] == "--- a/./rtl/alu.v" and shown[7] == "+++ b/./rtl/alu_tb.v", shown
    assert shown[3] == f"--- {root} looks like a header" and shown[4] == f"+++ {root} too", "body altered"
    assert shown[5] == "@@ -1 +1 @@ not a hunk", shown
    print("pending_diff_ids: outside-target entries dropped; globals restored: OK")

    # --- HDL source check ---
    if shutil.which("iverilog") is None:
        print("check_hdl_sources: SKIPPED (iverilog not on PATH)")
    else:
        rtl = ws.root / "rtl"
        assert sandbox.check_hdl_sources([rtl / "alu.v", rtl / "alu_tb.v"]) is None
        (outside / "secret.txt").write_text("SECRET\n", encoding="utf-8")
        sec = str(outside / "secret.txt").replace("\\", "/")
        attacks = {
            "fopen": 'module t; integer f; initial f = $fopen("/proc/self/environ", "r"); endmodule\n',
            "readmemh": 'module t; reg [7:0] m [0:3]; initial $readmemh("/etc/passwd", m); endmodule\n',
            "include": f'`include "{sec}"\nmodule t; endmodule\n',
            "macro include": f'`define INC `include "{sec}"\nmodule t; `INC endmodule\n',
            "token paste": 'module t; initial $f``open("x"); endmodule\n',
            "macro-built task": '`define F(x) $f``x\nmodule t; initial `F(open)("y"); endmodule\n',
            "system": 'module t; initial $system("id"); endmodule\n',
            "dumpfile": 'module t; initial $dumpfile("/tmp/x.vcd"); endmodule\n',
            # Quoted comment markers must not hide code from the scan (Codex review).
            "quoted //": 'module t; integer f; initial begin $display("//"); f = $fopen("/etc/passwd", "r"); end endmodule\n',
            "quoted /*": 'module t; integer f; initial begin $display("/*"); f = $fopen("/etc/passwd", "r"); $display("*/"); end endmodule\n',
            "quoted // + include": f'module t; initial $display("//"); endmodule\n`include "{sec}"\n',
            # A macro named after a directive must not re-enable it (Codex review).
            "define include": f'`define include 1\n`include "{sec}"\nmodule t; endmodule\n',
            # Order-dependent `ifdef: hidden from a check that sees the `define
            # first, live when compiled in another order (Codex review, task 14).
            "ifdef-hidden": '`define SAFE\nmodule t; integer f; initial begin\n`ifdef SAFE\n$display("ok");\n'
                            '`else\nf = $fopen("/etc/passwd", "r");\n`endif\nend endmodule\n',
            "macro task name": '`define T fopen\nmodule t; integer f; initial f = $`T("x", "r"); endmodule\n',
        }
        for label, src in attacks.items():
            f = rtl / "attack.v"
            f.write_text(src, encoding="utf-8")
            reason = sandbox.check_hdl_sources([f])
            assert reason, f"not blocked: {label}"
        # Conservative by design: even a mention inside a string is refused.
        (rtl / "attack.v").write_text('module t; initial $display("no $fopen here"); endmodule\n', encoding="utf-8")
        assert sandbox.check_hdl_sources([rtl / "attack.v"])
        (rtl / "attack.v").unlink()
        print(f"check_hdl_sources: sample passes; {len(attacks)} file-access tricks blocked: OK")

        # End to end through the guarded tool: the sample fails on XOR, an attack is blocked.
        r = impls["testbench_runner"](module_path="rtl/alu.v", tb_path="rtl/alu_tb.v")
        assert r["status"] == "fail_or_unknown" and "FAIL: 1 ALU" in r["summary"], r
        assert str(ws.root) not in str(r) and str(ws.root.resolve()) not in str(r), "workspace path leaked"
        (rtl / "evil_tb.v").write_text(attacks["fopen"], encoding="utf-8")
        r = impls["testbench_runner"](module_path="rtl/alu.v", tb_path="rtl/evil_tb.v")
        assert r["status"] == "blocked", r
        print("guarded testbench_runner: sample runs (XOR FAIL), $fopen testbench blocked: OK")

        # rtl_dir lets the runner compile more than the named files, so ANY
        # unsafe HDL file in the workspace blocks the run, not just named ones.
        (rtl / "evil_tb.v").unlink()
        r = impls["testbench_runner"](tb_path="rtl/alu_tb.v", rtl_dir="rtl")
        assert r["status"] == "fail_or_unknown" and "alu.v" in r["compiled_files"], r
        (rtl / "deep").mkdir()
        (rtl / "deep" / "helper.v").write_text(attacks["readmemh"], encoding="utf-8")
        for kwargs in ({"tb_path": "rtl/alu_tb.v", "rtl_dir": "rtl"}, {"tb_path": "rtl/alu_tb.v", "module_path": "rtl/alu.v"}):
            r = impls["testbench_runner"](**kwargs)
            assert r["status"] == "blocked", (kwargs, r)
            # debug_failing_test simulates too: same guard, stopped before any model call.
            r = impls["debug_failing_test"](**kwargs)
            assert r["status"] == "not_debuggable" and r["testbench_status"] == "blocked" or r["status"] == "blocked", (kwargs, r)
        # verify_loop simulates too: same whole-workspace guard, before any copy or model call.
        r = impls["verify_loop"](goal="add NOR", module_path="rtl/alu.v", tb_path="rtl/alu_tb.v")
        assert r["status"] == "blocked", r
        shutil.rmtree(rtl / "deep")
        r = impls["verify_loop"](goal="add NOR", module_path="rtl/alu.v", tb_path="rtl/alu_tb.v",
                                 project_dir=str(outside))
        assert r["status"] == "error" and "outside" in r["message"], r
        # ...and the compile check runs on the temp copy's own inputs, every compile.
        seen_inputs = []
        real_vl = sandbox.TOOL_IMPLS["verify_loop"]
        sandbox.TOOL_IMPLS["verify_loop"] = lambda **kw: seen_inputs.append(kw.get("compile_check")) or {"status": "ok"}
        try:
            _, impls_v = sandbox.guarded_tools(ws)
            impls_v["verify_loop"](goal="g", module_path="rtl/alu.v", tb_path="rtl/alu_tb.v")
        finally:
            sandbox.TOOL_IMPLS["verify_loop"] = real_vl
        assert seen_inputs == [sandbox.check_hdl_sources], seen_inputs
        print("guarded testbench_runner: rtl_dir works in the workspace; an unsafe file anywhere blocks it: OK")
        print("guarded verify_loop: workspace guard, confined project_dir, compile_check passed on: OK")
finally:
    shutil.rmtree(ws.root, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)

print("\nSANDBOX TESTS PASSED")
