// Stand-in ALU for the testbench_auditor regression test, written for this
// repo (not the original design). Same interface and ctrl encoding as the
// RISC-V ALU that ALU_tb_shift_bug.v targets; follows RV32I, so shifts use
// only the low 5 bits of b. The shift arms must stay on lines 34-36.
`timescale 1ns / 1ps

module ALU (
    input  wire [31:0] a,
    input  wire [31:0] b,
    input  wire [3:0]  ctrl,
    output reg  [31:0] result,
    output wire        zero
);

    // 0000 add
    // 0001 sub
    // 0010 and
    // 0011 or
    // 0100 xor
    // 0101 sll  (by b[4:0])
    // 0110 srl  (by b[4:0])
    // 0111 sra  (by b[4:0])
    // 1000 slt  (signed)
    // 1001 sltu (anything else -> 0)

    assign zero = (result == 32'b0);
    always @(*) begin
        case (ctrl)
            4'b0000: result = a + b;
            4'b0001: result = a - b;
            4'b0010: result = a & b;
            4'b0011: result = a | b;
            4'b0100: result = a ^ b;
            4'b0101: result = a << b[4:0];
            4'b0110: result = a >> b[4:0];
            4'b0111: result = $signed(a) >>> b[4:0];
            4'b1000: result = {31'b0, $signed(a) < $signed(b)};
            4'b1001: result = {31'b0, a < b};
            default: result = 32'b0;
        endcase
    end

endmodule
