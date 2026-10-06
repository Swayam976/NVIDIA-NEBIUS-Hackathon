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
    assert len(names) == 15
    print("guarded_tools: apply_diff withheld from the model, 15 tools offered: OK")

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
        })
        assert sandbox.pending_diff_ids(ws) == ["good"]
        assert "evil" not in module_modifier._load_pending()
        module_modifier._save_pending({})
    assert memory.MEMORY_DIR == MEMORY_DIR, "activate() must restore the globals"
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
finally:
    shutil.rmtree(ws.root, ignore_errors=True)
    shutil.rmtree(outside, ignore_errors=True)

print("\nSANDBOX TESTS PASSED")
