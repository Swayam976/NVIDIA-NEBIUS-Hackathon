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
| 6 | GitHub Actions cron for daily_brief via Token Factory (key in Actions secrets) | Claude | done |
| 7 | Free demo hosting on Streamlit Community Cloud (sample RTL, password + request cap) | Claude | done |
| 8 | README polish, demo script, Devpost write-up | Swayam + Claude | todo |
| 9 | Record <=3 min demo video | Swayam | todo |
| 10 | apply_diff gate: fail closed with no confirm handler; prompt shows real diff | Claude | done |
| 11 | Live audit of all 16 skills via CLI (RISC-V files), report + ranked fixes | Claude | done |
| 12 | Audit fixes 1-4: testbench summary, hazard checker, grounded spec drafts, git-skill errors + mtime fallback | Claude | done |
| 13 | Audit fixes 5-7: waveform_summarizer dependency, cross_project_linker noise, isa_spec_cross_referencer RV32I mode | Claude | done |
| 14 | testbench_runner multi-file designs + program files; spec_drafting_assistant flags names not in the RTL | Claude | done |
| 15 | New skill: debug_failing_test (run tb, locate first mismatch in VCD, root cause, fix as pending diff) | Claude | done |
| 16 | New skill: testbench_auditor (expected-value model vs RTL/spec, report only) | Claude | done |
| 17 | New skill: verify_loop (closed loop: change -> test -> debug in a temp copy, max 3 rounds, one gated diff) | Claude | done |

## Handoff notes
(Newest first. Who, what changed, what's next, rejected review findings and why.)

- 2026-10-06, Claude, task 17 done: verify_loop (tools/verify_loop.py), 19
  skills. Copies the project (project_files: generated/VCS trees skipped,
  <=3000 files / 50 MB) plus Vivado mem_init_files into a temp folder; all
  edits go through a guard that refuses writes outside the copy and edits
  that ADD file/process access (tools/hdl_safety.py, shared with the web
  sandbox's allowlist). Round 1: modify_module-style edit of the RTL, then of
  every testbench that instantiates it (max 3); every testbench whose design
  includes the module runs each round (+ lint on edited RTL). Done = target
  tb passes + everything that passed before still passes + every edited tb
  passes; pre-broken untouched tbs are reported as "unverified". Failures:
  compile error -> one fix call on the file iverilog blames; FAIL ->
  diagnose_failure (debug_failing_test's core, now split out) + one fix call;
  max 3 rounds, last round diagnosis only. Budget per round <= 4 calls,
  counted by llm.count_model_calls. All inputs re-hashed before staging;
  ONE combined pending diff (module_modifier.stage_pending, multi-file
  entries: one fingerprint over all files, all-or-nothing apply with
  byte-exact rollback incl. a truncated file, failed restores reported).
  Gate logic unchanged: CLI preview/approve/apply and the web panel just
  handle several files (panel header scrub now walks hunks: sandbox.
  display_diff). Temp folder deleted on every exit path; no git anywhere.
  Codex round 1: rollback of the failing file, hidden restore failures,
  edited tbs must pass (fixed); round 2: model HDL simulated unreviewed in
  the CLI (blocking; fixed via hdl_safety), mem_init_files outside the copy,
  stale non-edited inputs (fixed). 2 rounds used. New check:
  tests/test_verify_loop.py (also re-run test_sandbox, test_apply_gate).

- 2026-10-06, Claude, task 16 done: testbench_auditor (tools/debugging.py),
  18 skills now. Report only: never writes; fixes go through modify_module
  -> apply_diff (gate unchanged) if Swayam asks. Picks the RTL the testbench
  instantiates via design_files (module_path or rtl_dir), optional spec.
  Extracts every expected-value computation (expected/exp/golden/ref/model...
  assignments; strings and comments blanked; multi-line and several per line
  handled; case label from the same or the preceding line) and numbers them
  E1..En. A deterministic rule flags shift amounts that differ from the RTL
  arm with the SAME case label (tb full `b` vs RTL `b[4:0]`); no label or
  ambiguous RTL -> no rule finding. One reasoning call (thinking=True, 16k,
  one retry without reasoning) reviews all computations against the
  line-numbered RTL/spec; replies keyed by E-id (tb_line only if the line has
  a single computation), rtl_ref checked against files shown, severity
  sanitised; rule findings the model agrees with get confirmed_by_model.
  Model outage keeps rule findings. Truncated/omitted sources -> coverage
  "partial" in the report. Live on the real RISC-V ALU (direct + CLI):
  "3 mismatch(es) in 11 expected-value computation(s)", ALU_tb.v:47/48/49 ->
  ALU.v:34/35/36, all confirmed by the model, ~6-8 s, files unchanged.
  Regression fixture: tests/fixtures/riscv_alu/ (copies of the real buggy
  ALU_tb.v and ALU.v). Codex round 1: borrowed RTL case, multi-line/same-line
  assignments, block comments (fixed); round 2: label on the preceding line +
  all RTL shifts tracked, per-computation ids, partial-coverage report
  (fixed). 2 rounds used. New check: tests/test_auditor.py.
  Self-found after review: the web sandbox let spec_path "rv32i" through
  unconfined for every tool; now only isa_spec_cross_referencer gets that
  keyword (auditor resolves it inside the workspace; test_sandbox covers it).

- 2026-10-06, Claude, task 15 done: debug_failing_test (tools/debugging.py),
  17 skills now. Reuses testbench_runner (new internal vcd_out: compiles in
  one generated $dumpvars module, user files untouched), waveform_summarizer
  (window around the first failure) and modify_module (fix = pending diff;
  only apply_diff, behind the gate, writes). One reasoning call
  (json_completion thinking=True, 16k budget) + one retry without reasoning
  if cut off; modify_module adds its usual edit call. Locates the earliest
  failing line in the VCD by matching every logged name=value (radix read
  from the testbench's own $display formats; ambiguous radix, x/z values,
  missing or tied signals -> no location claimed, reason given). Passing or
  not-runnable tests stop before any model call. Live on the real RISC-V
  ALU (CLI + direct, 4 runs): root cause ALU_tb.v:47, high confidence,
  "expected shifts by full b; RTL correctly uses b[4:0]", failure at
  1,000,000 ps, one-hunk 3-line diff; agent asked before applying; files
  unchanged. Also: modify_module now undoes whitespace-only/blank-line edits
  it wasn't asked for (token-aware, string contents kept) - it had dropped
  an unrelated blank line in the live diff. Codex round 1: string spaces in
  the whitespace guard, earliest failure, %h/%b radix (fixed); round 2:
  conflicting radixes, tied signals (fixed). 2 rounds used.
  New check: tests/test_debugging.py.

- 2026-10-06, Claude, task 14 done. testbench_runner: rtl_dir (or module_path)
  -> compiles only modules the testbench instantiates (rtl_files.design_files),
  stages .mem/.hex/.dat program files (tb folder first, then its parent, then
  Vivado's mem_init_files) into the run dir, -g2012 only for .sv, retries
  with files for modules iverilog reports unknown, never guesses between
  duplicate definitions, reports missing program files (missing_data_file),
  timeouts, $fatal/non-zero exit, and same-named program files with
  different contents. spec_drafting_assistant: prompt also carries instance
  connections; every name in the draft is checked (case-sensitive, strings
  excluded) and unknown ones returned as unverified_names. Web sandbox:
  every HDL file in the workspace is checked, plus the exact compiler inputs
  in compile order before each attempt (compile_check hook, not in schema);
  raw-text system-task scan + no $`MACRO names. Verified on real designs:
  RISC-V pipelined_cpu_top_tb PASS (26 checks, 17 files); SIMT 13/19 pass.
  Findings for Swayam (project/testbench issues, not RTL, Vivado would hit
  them too): global.mem, pop.mem, relaunch.mem, idle.mem don't exist
  anywhere; simt_grid_tb never passes PROGRAM to the core (runs a stale
  program.hex); simt_replay_tb passes PROGRAM as a port, not a parameter.
  Codex: 3 rounds as authorized, 10 findings, all fixed (round 1: ifdef
  order bypass in the sandbox [blocking], retry ambiguity, case/strings in
  name check, 25-name cut; round 2: include dirs on retry, strings in the
  dependency walk, single-letter names; round 3: same-named program files,
  -g2012 for .sv, exit code). Round-3 fixes are unreviewed (no round left).
  New check: tests/test_multifile.py. All suites pass locally + Debian 11.

- 2026-10-06, Claude, task 13 done (audit fixes 5-7). waveform_summarizer:
  built-in streaming VCD reader (tools/vcd.py, stdlib only) instead of
  vcdvcd, whose licence is Perl 5 (Artistic 1 / GPL 1); full-width values
  + hex/dec, real values typed, time unit, value at window start, exact
  leaf match preferred. cross_project_linker: identifier-aware tokens,
  stopwords, words every project shares dropped, explicit project mentions
  scored highest, single shared word no longer a link. isa_spec_cross_
  referencer: RV32I mode (spec_path omitted or 'rv32i'): opcode-literal scan
  over all RTL as a hint + one Nemotron JSON call; live on the RISC-V core
  5/5 runs = 37/40 (FENCE, ECALL, EBREAK missing). New llm.json_completion:
  thinking off for tool JSON calls (live: Nemotron spent all 8192 tokens
  reasoning, empty reply); hazard review uses it too. Compact reply format +
  one retry for unanswered instructions (live: a broken JSON string
  swallowed 29 entries). Codex round 1: literal absence as definitive
  missing, value_at_start off by one, big files dropped (all fixed).
  Round 2: real VCD values (fixed); "downgrade missing on any omitted file"
  PARTLY ACCEPTED: any cut/omitted file now counts, but "missing" stands
  when the opcode has no literal in any file (the scan reads all RTL), else
  the real core's correct FENCE/ECALL/EBREAK result would become unclear.
  The reply-format/retry change came after round 2 and is unreviewed.

- 2026-10-06, Claude, task 12 done (audit fixes 1-4). testbench_runner:
  headline + pass/fail counts + failure-field grouping (real ALU_tb: 70/30,
  ctrl 5/6/7), neutral RTL-vs-testbench hint, status derived from counts.
  hazard_sanity_checker: rule for removed forwarding/stall/flush logic (per
  file) + one Nemotron JSON review; "no flags" now reads "needs simulation".
  spec_drafting_assistant: prompt carries real module interfaces from the
  project RTL (tools/rtl_files.py; skips Vivado generated dirs, testbenches,
  task args) and must write TBD instead of inventing names. Git skills:
  errors surfaced (commit_to_summary no longer says "no commits" on a
  non-git dir); regression_spotter falls back to file mtimes for non-git
  folders and excludes testbenches/generated files in both modes. Live CLI
  recheck: all four now give correct answers on the RISC-V files.
  New check: tests/test_skills.py. Codex round 1: non-object JSON review,
  status/count mismatch (fixed). Round 2: task args as ports, cross-file
  masking in hazard rule, budget break (fixed). 2 rounds used.
  Incident: at 12:17 this checkout was switched to codex-work (task 4) while
  task 12 was uncommitted; nothing mixed (Codex committed only its files).
  Per Swayam: task 12 moved back to main via stash; codex-work now has its
  own worktree at ../hw-copilot-codex (CLAUDE.md/AGENTS.md assumed one).

- 2026-10-06, Claude, task 11 audit (14 real CLI sessions, Nemotron via
  Token Factory, RISC-V files; memory/pending store on temp copies; NPU N/A,
  not local). Works: project_state_tracker, decision_log, lint_checker,
  modify_module, apply_diff (gate declined, ALU.v untouched),
  next_step_suggester, daily_brief. Weak: testbench_runner (cut-off log
  tail, no counts -> model blamed the ALU), spec_drafting_assistant
  (invented signal names), cross_project_linker (junk tokens). Fails:
  hazard_sanity_checker (missed removed EX/MEM forwarding priority),
  waveform_summarizer (vcdvcd missing), commit_to_summary +
  regression_spotter (non-git dir reported as "no commits/changes"),
  changelog_generator (clear error; no git repos exist). Not run:
  isa_spec_cross_referencer (no ISA spec anywhere). Side finding for
  Swayam: ALU_tb golden model shifts by full b for SLL/SRL/SRA (ctrl 5-7).

- 2026-10-06, task 6 done: manual run 37424187039 on a18e5e1 succeeded
  (confirmed via GitHub API), brief in the run summary. Scheduled daily at
  02:30 UTC / 08:00 IST from now on. Watch the first scheduled run after
  2026-10-19 (ubuntu-latest -> Ubuntu 26 migration notice).

- 2026-10-06, Claude, task 6 first CI run (manual) failed in "Write brief":
  APIConnectionError "Connection error." Reproduced locally with a dummy
  key: a trailing newline in the key -> httpx LocalProtocolError -> SDK
  reports "Connection error" (a clean wrong key gives 401 instead), so the
  pasted Actions secret almost certainly has trailing whitespace. Fix:
  config strips whitespace from NEBIUS_* env values (blank -> default);
  brief prints the cause *type* only (message can echo the auth header).
  Tests added in test_brief.py. Codex: no findings. Next: re-run workflow.

- 2026-10-06, Swayam verified the live demo in a browser (no issues) ->
  task 7 done, "Working demo URL" ticked in PROJECT.md.

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
