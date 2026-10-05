# RISC-V Core

## Status
5-stage pipelined RV32I core in Vivado (top: `pipelined_cpu_top`), with
forwarding (`forward_unit`), load-use stall (`load_hazard_ctrl`) and branch
flush (`flush_logic`). An earlier multi-cycle `cpu_top` is also in the
project. `pipelined_cpu_top_tb` failures from the 2026-08-31 run (incl. the
"poison skipped" check) are fixed per Swayam (2026-10-06). The SIMT GPU core
(simt-gpu-core) grew out of this pipeline.

## Decisions
(none yet)

## Blockers
(none yet)
