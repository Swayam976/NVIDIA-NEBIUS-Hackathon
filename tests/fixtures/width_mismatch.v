`timescale 1ns / 1ps
// Deliberate WIDTHEXPAND: 1-bit compare assigned to a 32-bit output.
module width_mismatch (
    input  wire [31:0] a,
    input  wire [31:0] b,
    output wire [31:0] lt
);
    assign lt = a < b;
endmodule
