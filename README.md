# Hardware Design Copilot

A personal AI assistant for hardware design work, built for the Nebius x NVIDIA
Global AI Hackathon (Personal AI track).

It keeps persistent memory of your active RTL projects, and gives an NVIDIA
Nemotron model (served through Nebius Token Factory) a set of tools to read,
reason about, and — with your explicit approval — edit your Verilog.

## Projects it tracks

- RISC-V core
- SIMT GPU core
- Micro-NPU
- MXINT8 GEMM accelerator

Each has a seed file under `memory/projects/`. Edit these to match reality
before your first run — they're starting points, not fiction.

## Setup

1. **Get credits**: join the [Nebius Builder Program](https://dev.nebius.com/builders)
   and claim your Token Factory + AI Cloud credits.
2. **Get an API key**: create one at the
   [Token Factory console](https://tokenfactory.nebius.com).
3. **Install dependencies**:
   ```bash
   python -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   ```
4. **Configure**:
   ```bash
   cp .env.example .env
   # edit .env and paste in your NEBIUS_API_KEY
   ```
5. **Run**:
   ```bash
   python -m src.copilot.cli
   ```

## Architecture

```
src/copilot/
  config.py      - env/config loading
  memory.py      - reads/writes memory/projects/*.md
  llm.py         - Nemotron client + tool-calling loop
  tools/         - the 22 skills, as callable tools with JSON schemas
  cli.py         - REPL entrypoint
memory/projects/ - one markdown file per hardware project (persistent state)
```

## Skills implemented

| Skill | What it does |
|---|---|
| `project_state_tracker` | Reports current status/blockers for a project |
| `decision_log` | Appends a design decision + rationale |
| `cross_project_linker` | Surfaces related context across projects |
| `testbench_runner` | Runs a Verilog testbench (iverilog/vvp) on single- or multi-file designs, staging program files as Vivado does |
| `lint_checker` | Runs Verilator lint on a module |
| `hazard_sanity_checker` | Heuristic scan of a diff for hazard red flags |
| `waveform_summarizer` | Summarizes a VCD dump around a signal/time range |
| `modify_module` | Generates a diff for a requested RTL change (review only) |
| `apply_diff` | Writes a previously generated diff to disk — **asks for confirmation** |
| `isa_spec_cross_referencer` | Checks which RV32I instructions the RTL implements (or diffs a spec file's mnemonics against it) |
| `spec_drafting_assistant` | Drafts spec/README text from project state + real RTL interfaces and wiring; flags names not in the RTL |
| `changelog_generator` | Turns git history into a readable changelog |
| `commit_to_summary` | "What did I do this week" from git log |
| `regression_spotter` | Flags commits touching modules with existing tests |
| `next_step_suggester` | Proposes the next concrete task for a project |
| `daily_brief` | Rolls up status/blockers across all tracked projects |
| `debug_failing_test` | Runs a failing testbench, finds the first mismatch in the waveform, names the root cause (file:line, confidence) and proposes a fix as a pending diff for `apply_diff` |
| `testbench_auditor` | Checks every expected-value computation in a testbench against the RTL (and spec); reports mismatches with file:line, changes nothing |
| `generate_rtl` | Creates a NEW module from plain-English requirements: interface spec + listed assumptions, repo style, lint + compile + interface check in a temp copy (max 3 rounds), then a new-file diff for `apply_diff` — never overwrites, never writes outside the repo |
| `generate_testbench` | Creates a NEW self-checking testbench (reset, one test per requirement, edge cases, PASS/FAIL + summary, VCD), audits its expected values, runs it in a temp copy and, if it fails, says whether the RTL or the testbench is wrong (file:line); new-file diff for `apply_diff` |
| `create_module` | "Create module X that does Y" end to end: spec + assumptions → RTL → self-checking testbench → lint + simulation with debug-and-fix rounds (`verify_loop`, max 3) in a temp copy, with a coverage gate (one check per requirement) and a final re-audit → ONE approval prompt listing every new file |
| `verify_loop` | Closed loop for one change: in a temp copy, edits the RTL and the testbenches that use it, runs every affected testbench plus lint, debugs and fixes failures (max 3 rounds), then hands ONE combined diff to `apply_diff` — your files are untouched until you say yes |

## Web demo

Live: https://nvidia-nebius-hackathon-kesr7gfcpbu3lgqhnuefra.streamlit.app/ (password-protected; ask the author for access)

`demo/streamlit_app.py` is a password-protected web UI, deployed on Streamlit
Community Cloud (main file `demo/streamlit_app.py`; `packages.txt` installs
iverilog + Verilator). Set these app secrets:

```toml
NEBIUS_API_KEY = "..."
DEMO_PASSWORD  = "..."
```

Each visitor gets a throwaway workspace with a small sample ALU. Tools are
confined to it, lint and simulation refuse file-access system tasks and
`` `include ``, and per-session/daily message caps protect API credits. Edits are
proposed as diffs and only written when the visitor clicks **Approve**.
Run it locally with `pip install -r demo/requirements.txt` then
`streamlit run demo/streamlit_app.py`.

## Safety note

`apply_diff` is the only tool that writes to your actual RTL files. The CLI
always prints the diff and requires a `y` confirmation before it runs — this
is intentional and shouldn't be bypassed even in a rush before the deadline.
