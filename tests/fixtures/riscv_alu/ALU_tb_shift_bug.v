`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 08/27/2026 09:53:43 PM
// Design Name: 
// Module Name: ALU_tb
// Project Name: 
// Target Devices: 
// Tool Versions: 
// Description: 
// 
// Dependencies: 
// 
// Revision:
// Revision 0.01 - File Created
// Additional Comments:
// 
//////////////////////////////////////////////////////////////////////////////////


module ALU_tb(

    );
    
    reg [31:0] a, b, expected;
    wire [31:0] result;
    reg [3:0] ctrl;
    wire zero;
    
    ALU dut(.a(a), .b(b), .ctrl(ctrl), .result(result), .zero(zero));
    integer i, j;
    
    initial begin
        ctrl = 4'b0000;
        for(i = 0; i < 10; i = i + 1) begin
            for(j = 0; j < 10; j = j + 1) begin
                a = $random;
                b = $random;
                case (ctrl)
                    4'b0000: expected = a + b;
                    4'b0001: expected = a - b;
                    4'b0010: expected = a & b;
                    4'b0011: expected = a | b;
                    4'b0100: expected = a ^ b;
                    4'b0101: expected = a << b;
                    4'b0110: expected = a >> b;
                    4'b0111: expected = $signed(a) >>> b;
                    4'b1000: expected = $signed(a) < $signed(b);           
                    4'b1001: expected = a < b;
                    default : expected = 32'b0;
                endcase
                #5;
                if(result !== expected) 
                    $display("FAIL,a = %d, b = %d, ctrl = %d, result = %d, expected = %d", a, b, ctrl, result, expected);
                else
                    $display("PASS");

                #15;
            end
            ctrl = ctrl + 4'b0001;
        end
    end
endmodule
