# Sample ALU (web demo)

## Status
8-bit ALU in rtl/alu.v with self-checking testbench rtl/alu_tb.v. Spec ops:
ADD (000), SUB (001), AND (010), OR (011), XOR (100). Everything except XOR
is implemented.

## Decisions
(none yet)

## Blockers
- XOR (op 3'b100) is not implemented in rtl/alu.v, so alu_tb fails
