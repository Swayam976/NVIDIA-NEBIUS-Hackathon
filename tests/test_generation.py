"""generate_rtl offline (mocked Nemotron, real iverilog/Verilator) and the
new-file path through the apply_diff gate.

Cases: happy path (spec + assumptions, repo style, lint + compile, new-file
diff against /dev/null); a failure fixed on retry (compile, and lint); the
3-round cap; refusing to overwrite; paths outside the repo, with "..", under
.git/; repo byte-identical after a "no"; apply_diff refusing without
approval for new AND modified files; a "yes" creating exactly the shown file.
Skipped when iverilog is not on PATH.
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
from src.copilot.tools import docs, generation, module_modifier  # noqa: E402

if not shutil.which("iverilog"):
    print("generation: SKIPPED (iverilog not on PATH)")
    sys.exit(0)

STYLE = """module counter (
  input  wire       clk,
  input  wire       rst_n,
  input  wire       en,
  output reg  [3:0] count
);
  // Style sample for the generator: 2-space indent, active-low async reset.
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= 4'd0;
    end else if (en) begin
      count <= count + 4'd1;
    end
  end
endmodule
"""
SPEC = {
    "summary": "Register with enable.",
    "parameters": [{"name": "WIDTH", "default": "8", "description": "data width"}],
    "ports": [{"name": "clk", "direction": "input", "width": "1", "description": "clock"},
              {"name": "rst_n", "direction": "input", "width": "1", "description": "active-low reset"},
              {"name": "en", "direction": "input", "width": "1", "description": "load enable"},
              {"name": "d", "direction": "input", "width": "WIDTH", "description": "data in"},
              {"name": "q", "direction": "output", "width": "WIDTH", "description": "data out"}],
    "clock": "clk", "reset": {"name": "rst_n", "active": "low", "type": "async"},
    "latency": "q updates on the clock edge after en",
    "behavior": ["reset clears q", "en loads d"],
    "assumptions": ["Reset is asynchronous, like counter.v."],
}
GOOD = """module dreg #(
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
SYNTAX_BUG = GOOD.replace("q <= d;", "q <= d")
LINT_MARK = GOOD.replace("endmodule", "// LINTBAD\nendmodule")


class Model:
    def __init__(self, rtl: list[str], spec=SPEC):
        self.rtl, self.spec, self.calls = list(rtl), spec, []

    def create(self, **kw):
        self.calls.append(kw)
        system = kw["messages"][0]["content"]
        if system == docs._INTERFACE_SPEC_PROMPT:
            content = json.dumps(self.spec)
        elif system.startswith("You write synthesizable"):
            content = json.dumps({"verilog": self.rtl.pop(0), "notes": "done"})
        else:
            raise AssertionError(f"unexpected call: {system[:50]}")
        msg = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    def client(self):
        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create)))


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


work = Path(tempfile.mkdtemp(prefix="copilot_gen_test_"))
module_modifier._PENDING_DIFFS_PATH = work / "pending.json"
made = []
_mkdtemp = tempfile.mkdtemp


def recording_mkdtemp(*a, **kw):
    p = _mkdtemp(*a, **kw)
    if kw.get("prefix") == "copilot_generate_":
        made.append(Path(p))
    return p


def repo(name: str) -> Path:
    root = work / name
    (root / "rtl").mkdir(parents=True)
    (root / "rtl" / "counter.v").write_text(STYLE, encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    return root


def gen(root: Path, model: Model, target="rtl/dreg.v", **kw):
    before = tree(root)
    made.clear()
    client = model.client()
    with mock.patch.object(docs, "get_client", return_value=client), \
         mock.patch.object(generation, "get_client", return_value=client), \
         mock.patch.object(generation.tempfile, "mkdtemp", side_effect=recording_mkdtemp):
        r = generation.generate_rtl(requirements="A WIDTH-bit register with load enable and active-low reset.",
                                    module_name="dreg", target_path=target, repo_root=str(root), **kw)
    assert tree(root) == before, "generate_rtl never writes the repo"
    assert not any(p.exists() for p in made), "temp copy deleted"
    return r


try:
    # --- happy path ---
    root = repo("r1")
    m = Model([GOOD])
    r = gen(root, m)
    assert r["status"] == "passed" and len(r["rounds"]) == 1 and r["model_calls"] == 2, r
    assert r["assumptions"] == ["Reset is asynchronous, like counter.v."] and r["spec"]["ports"][0]["name"] == "clk"
    assert r["style"]["clock"] == "clk" and r["style"]["reset"] == "rst_n" and r["style"]["reset_active"] == "low"
    assert r["style"]["indent"] == "2 spaces" and r["style"]["sample_files"] == ["rtl/counter.v"], r["style"]
    assert r["compile"] == "ok" and r["file"] == "rtl/dreg.v" and r["temp_copy_removed"], r
    assert r["diff"].startswith("--- /dev/null\n+++ b/") and "+module dreg #(" in r["diff"], r["diff"][:200]
    rtl_prompt = m.calls[1]["messages"]
    assert "Verilog-2005" in rtl_prompt[0]["content"] and "counter.v" in rtl_prompt[1]["content"], "style sample sent"
    entry = module_modifier._load_pending()[r["pending_diff"]["diff_id"]]
    assert entry["create"] is True and Path(entry["repo_root"]) == root.resolve(), entry
    print("generate_rtl: spec + assumptions, repo style, compile + lint, new-file diff staged: OK")

    # --- the gate for new files ---
    diff_id = r["pending_diff"]["diff_id"]
    target = root / "rtl" / "dreg.v"
    before = tree(root)
    assert module_modifier.apply_diff(diff_id)["status"] == "declined" and not target.exists(), "no approval, no file"
    with mock.patch.object(builtins, "input", return_value="n"), mock.patch("sys.stdout", new=io.StringIO()) as out:
        assert cli.confirm_tool_call("apply_diff", {"diff_id": diff_id}) is False
    assert "--- /dev/null" in out.getvalue() and "+module dreg" in out.getvalue(), "new file shown in full before asking"
    assert module_modifier.apply_diff(diff_id)["status"] == "declined" and tree(root) == before, "a 'no' changes nothing"
    # Approved, but the file appeared meanwhile: never overwritten.
    _, fp = module_modifier.preview_pending_diff(diff_id)
    module_modifier.approve_pending_diff(diff_id, fp)
    target.write_text("// someone else's file\n", encoding="utf-8")
    res = module_modifier.apply_diff(diff_id)
    assert res["status"] == "declined" and "already exists" in res["message"], res
    assert target.read_text(encoding="utf-8") == "// someone else's file\n"
    target.unlink()
    # A pending entry tampered to point outside its repo root is refused at apply time.
    pending = module_modifier._load_pending()
    pending[diff_id]["module_path"] = str(work / "outside.v")
    module_modifier._save_pending(pending)
    _, fp = module_modifier.preview_pending_diff(diff_id)
    module_modifier.approve_pending_diff(diff_id, fp)
    res = module_modifier.apply_diff(diff_id)
    assert res["status"] == "declined" and "outside the repo root" in res["message"] and not (work / "outside.v").exists()
    pending = module_modifier._load_pending()
    pending[diff_id]["module_path"] = str(target)
    module_modifier._save_pending(pending)
    # "yes": exactly the shown file is created, folders included.
    with mock.patch.object(builtins, "input", return_value="y"), mock.patch("sys.stdout", new=io.StringIO()):
        assert cli.confirm_tool_call("apply_diff", {"diff_id": diff_id}) is True
    assert module_modifier.apply_diff(diff_id)["status"] == "ok"
    assert target.read_bytes() == GOOD.encode(), "created byte-for-byte as shown (LF)"
    print("apply_diff new files: no approval -> nothing; 'no' -> nothing; never overwrites; root re-checked; 'yes' creates: OK")

    # Modified files still need approval too (unchanged behaviour).
    mod_id = module_modifier.stage_pending([(str(target), GOOD + "// edit\n")])
    assert module_modifier.apply_diff(mod_id)["status"] == "declined"
    assert target.read_bytes() == GOOD.encode()
    module_modifier.discard_pending_diff(mod_id)
    # A new file nested in folders that don't exist yet: created, and on a failed
    # write the folders made for it are removed again.
    nested = root / "rtl" / "deep" / "er" / "x.v"
    nid = module_modifier.stage_pending([(str(nested), "module x; endmodule\n")], creates={str(nested)}, repo_root=str(root))
    _, fp = module_modifier.preview_pending_diff(nid)
    module_modifier.approve_pending_diff(nid, fp)
    with mock.patch.object(module_modifier, "_open", side_effect=OSError("disk full")):
        assert module_modifier.apply_diff(nid)["status"] == "error"
    assert not (root / "rtl" / "deep").exists(), "folders made for the new file were removed"
    # Codex review: a file that appears between the check and the write is never
    # overwritten (exclusive create) and never deleted by the rollback.
    racy = root / "rtl" / "racy.v"
    rid = module_modifier.stage_pending([(str(racy), "module racy; endmodule\n")], creates={str(racy)}, repo_root=str(root))
    _, fp = module_modifier.preview_pending_diff(rid)
    module_modifier.approve_pending_diff(rid, fp)
    real_open = module_modifier._open

    def someone_writes_first(path, mode):
        Path(path).write_text("// theirs\n", encoding="utf-8")
        return real_open(path, mode)

    with mock.patch.object(module_modifier, "_open", side_effect=someone_writes_first):
        res = module_modifier.apply_diff(rid)
    assert res["status"] == "error" and racy.read_text(encoding="utf-8") == "// theirs\n", res
    racy.unlink()
    module_modifier.discard_pending_diff(rid)
    print("apply_diff: modified files still gated; failed new-file write leaves no folders; racing file kept: OK")

    # --- a compile error, fixed on retry ---
    root = repo("r2")
    m = Model([SYNTAX_BUG, GOOD])
    r = gen(root, m)
    assert r["status"] == "passed" and len(r["rounds"]) == 2 and r["rounds"][0]["compile"] == "failed", r["rounds"]
    retry = m.calls[2]["messages"][-1]["content"]
    assert "failed these checks" in retry and "syntax error" in retry.lower(), retry
    assert "The lines they point at:" in retry and "|     end else if (en) begin" in retry, "numbered context shown"
    assert "smallest change" in retry
    assert r["model_calls"] == 3
    # --- a lint error, fixed on retry ---
    root = repo("r3")
    real_lint = generation.lint_checker

    def lint(path, **kw):
        if "LINTBAD" in Path(path).read_text(encoding="utf-8"):
            return {"status": "issues_found", "warning_count": 1, "warnings": ["%Error: dreg.v:17:1: bad thing"],
                    "errors": ["%Error: dreg.v:17:1: bad thing"]}
        return real_lint(path, **kw)

    with mock.patch.object(generation, "lint_checker", side_effect=lint):
        r = gen(root, Model([LINT_MARK, GOOD]))
    assert r["status"] == "passed" and len(r["rounds"]) == 2 and r["rounds"][0]["compile"] == "ok", r["rounds"]
    assert r["rounds"][0]["errors"] == ["%Error: dreg.v:17:1: bad thing"], r["rounds"][0]
    print("generate_rtl: compile error and lint error each fixed on the retry: OK")

    # --- the 3-round cap ---
    root = repo("r4")
    r = gen(root, Model([SYNTAX_BUG] * 3))
    assert r["status"] == "still_failing" and len(r["rounds"]) == 3 and r["model_calls"] == 4, r
    assert r["pending_diff"] and "still fails" in r["note"], r
    # File/process access in generated RTL is never even compiled.
    root = repo("r5")
    evil = GOOD.replace("endmodule", 'initial $system("calc");\nendmodule')
    r = gen(root, Model([evil, GOOD]))
    assert r["status"] == "passed" and r["rounds"][0]["compile"] == "not run" and "$system" in r["rounds"][0]["errors"][0]
    # Codex review: unsafe in every round -> reported, and nothing is staged.
    pending_before = set(module_modifier._load_pending())
    r = gen(repo("r5b"), Model([evil] * 3))
    assert r["status"] == "blocked" and "$system" in r["message"] and r["pending_diff"] is None, r
    assert set(module_modifier._load_pending()) == pending_before
    print("generate_rtl: stops at 3 rounds (staged, flagged); file/process access never compiled: OK")

    # --- Codex review ---
    # (3) the generated interface must match the spec shown; a mismatch is fed back.
    root = repo("r7")
    wrong = GOOD.replace("input  wire             en,", "input  wire             load,").replace("else if (en)", "else if (load)")
    m = Model([wrong, GOOD])
    r = gen(root, m)
    assert r["status"] == "passed" and r["rounds"][0]["interface"] == "mismatch" and r["interface"] == "matches the spec"
    assert "Port en from the interface spec is missing." in r["rounds"][0]["errors"], r["rounds"][0]["errors"]
    assert "Port load is not in the interface spec." in m.calls[2]["messages"][-1]["content"]
    # ...parameter defaults and extra parameters too.
    root = repo("r7b")
    m = Model([GOOD.replace("parameter WIDTH = 8", "parameter WIDTH = 32,\n  parameter EXTRA = 1"), GOOD])
    r = gen(root, m)
    assert r["status"] == "passed" and len(r["rounds"]) == 2, r
    assert {"Parameter WIDTH defaults to 32, the spec says 8.",
            "Parameter EXTRA is not in the interface spec (use a localparam for internal constants)."} <= set(r["rounds"][0]["errors"])
    # (1) lint that can't run is never "passed", and isn't retried pointlessly.
    root = repo("r8")
    with mock.patch.object(generation, "lint_checker", return_value={"status": "unavailable", "message": "no verilator"}):
        r = gen(root, Model([GOOD]))
    assert r["status"] == "compiled_not_linted" and r["verification"] == "partial" and len(r["rounds"]) == 1, r
    assert "lint could not run" in r["note"], r["note"]
    # (2) connect_to files are linted explicitly: file name != module name, two modules per file.
    root = repo("r9")
    (root / "rtl" / "blocks.v").write_text(
        "module adder (input wire [7:0] a, input wire [7:0] b, output wire [7:0] s);\n  assign s = a + b;\nendmodule\n"
        "module other (input wire x, output wire y);\n  assign y = ~x;\nendmodule\n", encoding="utf-8")
    wrapper_spec = dict(SPEC, parameters=[], ports=[
        {"name": "a", "direction": "input", "width": "8"}, {"name": "b", "direction": "input", "width": "8"},
        {"name": "s", "direction": "output", "width": "8"}], assumptions=[])
    wrapper = ("module dreg (\n  input  wire [7:0] a,\n  input  wire [7:0] b,\n  output wire [7:0] s\n);\n"
               "  adder u_adder (.a(a), .b(b), .s(s));\nendmodule\n")
    r = gen(root, Model([wrapper], spec=wrapper_spec), connect_to=["rtl/blocks.v"])
    assert r["status"] == "passed" and r["lint"]["status"] in ("clean", "issues_found") and not r["lint"]["errors"], r
    print("generate_rtl: interface checked against the spec; lint gaps reported; connected files linted: OK")

    # --- refused before any model call ---
    root = repo("r6")
    for target, why in (("rtl/counter.v", "already exists"), (str(work / "elsewhere" / "x.v"), "outside the repo root"),
                        ("../x.v", "'..'"), ("rtl/../../x.v", "'..'"), (".git/hooks/x.v", ".git"),
                        ("rtl/notes.txt", ".v")):
        m = Model([])
        r = gen(root, m, target=target)
        assert r["status"] == "error" and why in r["message"] and m.calls == [], (target, r)
    print("generate_rtl: overwrite, outside/../.git paths and non-HDL targets refused before any model call: OK")
finally:
    shutil.rmtree(work, ignore_errors=True)

print("\nGENERATION TESTS PASSED")
