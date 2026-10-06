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
| 6 | GitHub Actions cron for daily_brief via Token Factory (key in Actions secrets) | Claude | in progress (Claude) |
| 7 | Free demo hosting on Streamlit Community Cloud (sample RTL, password + request cap) | Claude | in review (Swayam: verify chat flow) |
| 8 | README polish, demo script, Devpost write-up | Swayam + Claude | todo |
| 9 | Record <=3 min demo video | Swayam | todo |
| 10 | apply_diff gate: fail closed with no confirm handler; prompt shows real diff | Claude | done |

## Handoff notes
(Newest first. Who, what changed, what's next, rejected review findings and why.)

- 2026-10-06, Claude, task 7 deployed by Swayam: https://nvidia-nebius-hackathon-kesr7gfcpbu3lgqhnuefra.streamlit.app/
  Public sharing on; anonymous request -> Streamlit Cloud cookie handshake
  -> HTTP 200 app shell (checked with curl). App logic not verifiable
  without a browser + DEMO_PASSWORD: Swayam to run the ALU flow (testbench
  fails -> fix -> Approve -> passes), then tick "Working demo URL".

- 2026-10-06, Claude, task 7 code committed (in progress until deployed).
  Per Swayam: HF Spaces now needs PRO for Gradio (free = ZeroGPU only, 30d+
  accounts), so Streamlit Community Cloud; sample RTL only; password + caps.
  demo/streamlit_app.py: password gate (DEMO_PASSWORD secret, fails closed
  if unset), per-session workspace (repo memory + demo/sample ALU with a
  deliberate missing XOR), session cap 15 / daily cap 150 (secrets
  DEMO_SESSION_LIMIT / DEMO_DAILY_LIMIT). Model never gets apply_diff here;
  edits land only via the Pending changes panel Approve click (fingerprint
  from the rendered diff). src/copilot/sandbox.py: paths confined to the
  workspace (no traversal/absolute/hidden), project slugs validated, args
  filtered to schema, HDL check before lint/sim (directive allowlist on raw
  text, no token pasting, macros can't shadow directives, system-task
  allowlist on iverilog -E output, no stripping of comments/strings), and a
  lock swapping memory/pending globals per session. llm.run_agent_loop got
  extra_system; module_modifier got discard_pending_diff. TB prints
  op=3'b... (Nemotron misread op=100 as decimal in the first live run).
  Verified: live AppTest run with real Nemotron + iverilog (fail -> 1-line
  XOR diff -> Approve -> PASS); all 6 test scripts pass locally and on
  python:3.12-bullseye with Debian iverilog 11 / Verilator 4.038 (made the
  width-warning test version-agnostic). Codex round 1: quoted // hid
  $fopen (fixed: no stripping), reset bypassed cap (fixed). Round 2:
  `define include re-enabled include (fixed), scrubbed diff body could
  differ from applied content (fixed: header-only). 2 rounds used.
  Next: Swayam deploys on share.streamlit.io and sets secrets.

- 2026-10-06, Claude, task 6 replanned per Swayam: no Nebius AI Cloud.
  Removed the Serverless Job / Task Scheduler / MysteryBox scripts
  (supersedes the note below). Now .github/workflows/daily-brief.yml runs
  `python -m src.copilot.brief` at 02:30 UTC (08:00 IST) + manual dispatch;
  key from secrets.NEBIUS_API_KEY; optional repo vars NEBIUS_MODEL,
  NEBIUS_BASE_URL, BRIEF_TZ; brief goes to log + job summary; no PR trigger
  so forks never get the secret. Found: config/.env.example defaults pointed
  at https://api.tokenfactory.nebius.com (404) and an unverified model ->
  defaults now the verified us-central1 /v1/ URL and
  nemotron-3-super-120b-a12b. Verified: actionlint clean; workflow steps
  simulated in python:3.13 container with only the key set (exit 0).
  Next: Swayam adds the NEBIUS_API_KEY Actions secret, push, manual run.

- 2026-10-06, Claude, task 6 code committed (still in progress: needs a live
  Nebius run). Nebius Serverless Jobs have no cron (CLI ref, jobs docs,
  changelog to 2026-09), so per Swayam: Windows Task Scheduler ->
  scripts/run-daily-brief.sh in WSL (nebius CLI is Linux/macOS only) ->
  `nebius ai job create` on cpu-d3/2vcpu-8gb, python:3.12 image, entry
  script + local memory files via --inject-file, API key via --env-secret
  (MysteryBox, never in the job spec), brief to job logs. Entry clones
  GitHub main, so code must be pushed. src/copilot/brief.py = one tool-less
  Nemotron call (can't reach apply_diff). Verified: live brief locally;
  full job simulated in Docker python:3.12 (exit 0); trigger args checked
  offline with a stub CLI; flags checked vs nebius CLI 0.12.284 --help.
  Fixed UTC date in container -> BRIEF_DATE from local machine.
  Codex round 1: .env quote/comment parsing (fixed in shell; WSL python
  lacks python-dotenv), repo overrides not forwarded (fixed). Round 2:
  battery settings for laptop (fixed). Next: Swayam pushes, runs
  `nebius profile create`, creates MysteryBox secret; then dry-run, live
  run, register task.

- 2026-10-06, Claude, task 10 done (apply_diff gate, before task 6).
  run_agent_loop now fails closed: gated tools are declined when
  confirm_tool_call is None or the handler raises (old code applied diffs
  with no handler - verified by running the new test against it). CLI prompt
  prints the real diff from the pending store (current file vs proposed),
  and a "y" records approval bound to a SHA-256 fingerprint of path + shown
  file + proposed content; apply_diff refuses without a matching approval
  (also blocks direct calls that skip the loop). Write failure keeps the
  proposal. New check: python tests/test_apply_gate.py (11 cases).
  Codex round 1: approval not bound to shown diff (fixed), preview errors
  crash CLI (fixed). Round 2: write failure lost proposal (fixed).
  REJECTED round 2 "blocking": file could change in the microseconds between
  the fingerprint check and write_text - same-call window after a human
  approved the exact diff; closing it needs OS file locking other editors
  don't honour on Windows. Residual risk accepted; Swayam to confirm.

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
  (NEBIUS_BASE_URL=https://api.tokenfactory.us-central1.nebius.com/v1/ - corrected
  2026-10-06; the unregional URL without /v1 returns 404,
  NEBIUS_MODEL=nvidia/nemotron-3-super-120b-a12b); model called
  project_state_tracker and summarized mxint8-gemm memory. Fixed: CLI crashed
  printing non-cp1252 chars (U+2011) on Windows -> stdout reconfigured to UTF-8.
  Fixed: smoke_test.py wrote fake decisions into real memory/projects/ ->
  now runs on a temp copy. Codex review: 1 nit (temp dir leak) fixed.
  Open: *_REPO_PATH in .env still defaults (./repos/... missing) - waiting on
  Swayam for real checkout paths. Flag for later: llm.run_agent_loop skips the
  apply_diff gate entirely when confirm_tool_call is None (fail-open); CLI
  always passes it, but the loop should fail closed.
