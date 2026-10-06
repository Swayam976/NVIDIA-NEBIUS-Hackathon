`timescale 1ns / 1ps
// 8-bit ALU for the hw-copilot web demo.
// Spec ops: 000 ADD, 001 SUB, 010 AND, 011 OR, 100 XOR.
// XOR is not implemented yet, so alu_tb.v reports a failure.
module alu (
    input  wire [7:0] a,
    input  wire [7:0] b,
    input  wire [2:0] op,
    output reg  [7:0] y,
    output wire       zero
);
    always @(*) begin
        case (op)
            3'b000:  y = a + b;
            3'b001:  y = a - b;
            3'b010:  y = a & b;
            3'b011:  y = a | b;
            default: y = 8'h00;
        endcase
    end

    assign zero = (y == 8'h00);
endmodule
