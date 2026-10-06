`timescale 1ns / 1ps
// Self-checking testbench for alu.v; prints a single PASS or FAIL summary.
module alu_tb;
    reg  [7:0] a, b;
    reg  [2:0] op;
    wire [7:0] y;
    wire       zero;
    integer    errors = 0;

    alu dut (.a(a), .b(b), .op(op), .y(y), .zero(zero));

    task check(input [2:0] t_op, input [7:0] t_a, input [7:0] t_b, input [7:0] expected);
        begin
            op = t_op; a = t_a; b = t_b;
            #1;
            if (y !== expected || zero !== (expected == 8'h00)) begin
                $display("mismatch op=3'b%b a=8'h%h b=8'h%h: y=8'h%h, expected 8'h%h", t_op, t_a, t_b, y, expected);
                errors = errors + 1;
            end
        end
    endtask

    initial begin
        check(3'b000, 8'h12, 8'h34, 8'h46);
        check(3'b000, 8'hFF, 8'h01, 8'h00);
        check(3'b001, 8'h50, 8'h20, 8'h30);
        check(3'b010, 8'hF0, 8'h3C, 8'h30);
        check(3'b011, 8'hF0, 8'h0F, 8'hFF);
        check(3'b100, 8'hAA, 8'hFF, 8'h55);
        check(3'b100, 8'h5A, 8'h5A, 8'h00);
        if (errors == 0) $display("PASS: all ALU checks passed");
        else             $display("FAIL: %0d ALU check(s) failed", errors);
        $finish;
    end
endmodule
