`timescale 1ns / 1ps
//////////////////////////////////////////////////////////////////////////////////
// Company: 
// Engineer: 
// 
// Create Date: 08/27/2026 09:14:06 PM
// Design Name: 
// Module Name: ALU
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


module ALU(
    input [31:0]a, input [31:0]b, input[3:0]ctrl, output reg [31:0]result, output reg zero
    );
    
    always @ (*) begin
        case (ctrl)
            4'b0000: result = a + b;
            4'b0001: result = a - b;
            4'b0010: result = a & b;
            4'b0011: result = a | b;
            4'b0100: result = a ^ b;
            4'b0101: result = a << b[4:0];
            4'b0110: result = a >> b[4:0];
            4'b0111: result = $signed(a) >>> b[4:0];
            4'b1000: result = $signed(a) < $signed(b);           
            4'b1001: result = a < b;
            default : result = 32'b0;
        endcase
        
        if (result == 0)
            zero = 1;
        else 
            zero = 0;
    end
endmodule
