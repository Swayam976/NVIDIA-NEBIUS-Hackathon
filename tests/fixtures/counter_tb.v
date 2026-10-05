`timescale 1ns / 1ps
module counter_tb;
    reg clk = 1'b0;
    reg reset = 1'b1;
    wire [3:0] count;

    counter dut (.clk(clk), .reset(reset), .count(count));

    always #5 clk = ~clk;

    initial begin
        @(posedge clk); #1 reset = 1'b0;
        repeat (5) @(posedge clk);
        #1;
        if (count == 4'd5) $display("PASS count=%0d", count);
        else               $display("FAIL count=%0d expected 5", count);
        $finish;
    end
endmodule
