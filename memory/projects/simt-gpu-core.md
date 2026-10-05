# SIMT GPU Core

## Status
SIMT GPU core built from the RISC-V pipeline (riscv-core), in Vivado (top:
`pipelined_cpu_top`). 4 warps x 8 lanes, per-warp reconvergence stack for
branch divergence, kernel launch interface (grid/args), barriers, global
memory with coalescing, and CSRs. ~19 self-checking testbenches (barcount,
barexit, barphase, blocks, coalesce, conflict, csr, full, global, grid, idle,
launch, multiwarp, nested, pop, reduce, relaunch, replay, waves). The
`simt_barexit_tb` failures from the 2026-10-04 run ("never released" lanes)
are fixed per Swayam (2026-10-06); testbenches expected to pass.

## Decisions
(none yet)

## Blockers
(none yet)
