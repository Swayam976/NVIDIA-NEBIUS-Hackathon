# Tasks

Status: todo / in progress (owner) / in review / done
Deadline: Oct 30, 2026, 1:00pm EDT

| # | Task | Owner | Status |
|---|------|-------|--------|
| 1 | Real .env (Nebius key, *_REPO_PATH), one live Nemotron call via CLI | Claude | done |
| 2 | Update memory/projects/ seed files with real status (ask Swayam) | Claude | done |
| 3 | Install iverilog + verilator, confirm testbench_runner / lint_checker work | Claude | done |
| 4 | Unit tests for memory.py | Codex (codex-work) | todo |
| 5 | Real modify -> diff -> approve -> apply loop on one small GEMM change | Claude | todo |
| 6 | Serverless Job (cron) for daily_brief | Claude | todo |
| 7 | Serverless Endpoint for demo URL | Claude | todo |
| 8 | README polish, demo script, Devpost write-up | Swayam + Claude | todo |
| 9 | Record <=3 min demo video | Swayam | todo |

## Handoff notes
(Newest first. Who, what changed, what's next, rejected review findings and why.)

- 2026-10-06, Claude, task 3 done. Installed via MSYS2 (ucrt64):
  iverilog 13.0, Verilator 5.050; C:\msys64\ucrt64\bin appended to user PATH
  (new terminals only). Fixes: testbench_runner used /tmp (missing on
  Windows) -> per-call temp dir; lint_checker never found `verilator` on
  Windows (it's an extensionless Perl script) -> falls back to
  verilator_bin.exe with VERILATOR_ROOT derived from the install prefix.
  New check: python tests/test_verification.py (skips if tools missing).
  Verified on real RTL: ALU.v lint -> 2 WIDTHEXPAND (lines 37-38);
  ALU_tb.v runs but ctrl=7 vectors fail because the TB golden model shifts
  by full b (ALU_tb.v:49) while RTL correctly uses b[4:0] -> TB bug, Swayam's
  repo untouched. Codex round 1: prefer verilator_bin on Windows (fixed,
  though which() returned None here on Py3.13); round 2: "Exiting due to"
  filter could hide internal faults -> fixed + nonzero exit never "clean".
  Limitation for task 5: testbench_runner takes one module file, so
  multi-file designs (SIMT core) can't run through it yet.

- 2026-10-06, Claude, task 2 done. Per Swayam: dropped custom-isa and
  dsp-fpga; split simt-gpu-core out of riscv-core (renamed from
  riscv-simt-core). Config reads RISCV_REPO_PATH, SIMT_GPU_CORE_PATH,
  NPU_REPO_PATH, GEMM_REPO_PATH; system prompt, README, PROJECT.md,
  .env.example updated. Statuses written from Swayam's answers + RTL/sim logs
  under C:\Xilinx Projects. micro-npu RTL is on a remote server (not local);
  mxint8-gemm RTL not started (empty GEMM.xpr) -> task 5 needs GEMM RTL first.
  Codex review: no findings.

- 2026-10-06, Claude, task 1 done. Live Nemotron call via CLI works
  (NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com,
  NEBIUS_MODEL=nvidia/nemotron-3-super-120b-a12b); model called
  project_state_tracker and summarized mxint8-gemm memory. Fixed: CLI crashed
  printing non-cp1252 chars (U+2011) on Windows -> stdout reconfigured to UTF-8.
  Fixed: smoke_test.py wrote fake decisions into real memory/projects/ ->
  now runs on a temp copy. Codex review: 1 nit (temp dir leak) fixed.
  Open: *_REPO_PATH in .env still defaults (./repos/... missing) - waiting on
  Swayam for real checkout paths. Flag for later: llm.run_agent_loop skips the
  apply_diff gate entirely when confirm_tool_call is None (fail-open); CLI
  always passes it, but the loop should fail closed.
