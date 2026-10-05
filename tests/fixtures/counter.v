`timescale 1ns / 1ps
// 4-bit synchronous counter with active-high reset. Lint-clean under -Wall.
module counter (
    input  wire       clk,
    input  wire       reset,
    output reg  [3:0] count
);
    always @(posedge clk) begin
        if (reset) count <= 4'd0;
        else       count <= count + 4'd1;
    end
endmodule
