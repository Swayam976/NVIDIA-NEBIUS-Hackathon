# hw-copilot: project brief (read this first)

## What this is
A personal AI copilot for my hardware design work. Submission to the
Nebius x NVIDIA Global AI Hackathon, Personal AI track.
**Deadline: Oct 30, 2026, 1:00pm EDT.** Scope every change against it.

## Hardware projects it serves (live, mid-build)
- RISC-V/SIMT core: 5-stage pipelined RV32I, hazard handling
- Custom ISA: spec, simulator, Verilog, maybe a compiler
- Micro-NPU: DMA engine, systolic array, skewed FIFO, APB wrapper
- MXINT8 GEMM accelerator: 8x8 output-stationary systolic array, two-level accumulator (extends micro-NPU)
- DSP-FPGA: FIR filter / FFT core

## Architecture
- Agent core: NVIDIA Nemotron via Nebius Token Factory (OpenAI-compatible API), tool calling (`src/copilot/llm.py`)
- Memory: one structured file per project in `memory/projects/` (`src/copilot/memory.py`)
- Sandbox: OpenShell for shell / simulator / git access
- Scheduler: Nebius Serverless Job on cron for the daily/weekly brief
- Demo host: Nebius Serverless Endpoint
- Interface: CLI (`python -m src.copilot.cli`)

## The 16 skills
Project state tracker, Decision log, Cross-project linker, Testbench runner,
Waveform summarizer, Lint checker, Hazard sanity checker, Module modifier
(generates diff), apply_diff (approval-gated), ISA spec cross-referencer,
Spec drafting assistant, Changelog generator, Commit-to-summary, Regression
spotter, Next-step suggester, Daily/weekly brief.

## Hard constraints
1. `apply_diff` must always show the diff and wait for an explicit human yes.
   Never write code that auto-applies or auto-commits RTL changes. Never weaken
   or bypass the confirmation gate, including in tests or "demo mode".
2. The submitted system calls an NVIDIA open model on Nebius at runtime.
   No OpenAI/Anthropic models inside the product itself.
3. Verilog/SystemVerilog conventions by default.
4. Repo must stay public-ready: MIT license, README, no secrets (`.env` is gitignored).

## Acceptance criteria (Devpost)
- [ ] Working project on Nemotron via Token Factory
- [ ] Working demo URL
- [ ] <=3 min public YouTube demo video
- [ ] Public repo with OSS license + README
- [ ] Project description + feedback on Nebius/NVIDIA tools
- [ ] Builders & Brews city noted (if attended)

## Checks to run before calling anything done
- `python tests/smoke_test.py` (mocked LLM, no network)
- Any new unit tests under `tests/`
