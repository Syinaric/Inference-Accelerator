//processing element for the 8x8 output 
// one signed INT8 MAC per cycle, operands are passed east and south
//one output time is held in shadow register until the drain moves it out 

module pe #(
    parameter int DATA_WIDTH = 8, 
    parameter int ACC_WIDTH = 32 
)(
    input logic clk,
    input logic reset,
    input logic en,
    //west to east and north to south
    input logic signed  [DATA_WIDTH -1 :0] a_in,
    input logic signed  [DATA_WIDTH -1 :0] b_in,
    output logic signed [DATA_WIDTH -1 :0]a_out,
    output logic signed [DATA_WIDTH -1 :0]b_out,

    input logic first_in, 
    input logic last_in,
    output logic first_out, 
    output logic last_out,

   //drain 
    input logic drain_shift,
    input  logic signed [ACC_WIDTH-1:0]   drain_in,
    output logic signed [ACC_WIDTH-1:0]   drain_out

); 


    initial begin
         if (ACC_WIDTH < 2*DATA_WIDTH) begin 
            $fatal (1, "pe: ACC_WIDTH=%0d too narrow for DATA_WIDTH=%0d (need  >= %0d)",
             ACC_WIDTH, DATA_WIDTH, 2*DATA_WIDTH);
         end 
    end 

    logic signed [ACC_WIDTH - 1: 0] acc; 
    logic active; //in the acc window 
    logic cap ; // shadow registor, 1 cycle after last_in 
    logic signed [2*DATA_WIDTH- 1 : 0] product; 
    assign product = a_in*b_in; 

    always_ff @(posedge clk) begin  //always create flipflip 
        if (reset) begin 
            a_out <= '0; 
            b_out <= '0; 
            first_out <= 1'b0; 
            last_out <= 1'b0; 
            acc <= '0;
            active <= 1'b0;
            cap <= 1'b0;
            drain_out <= '0;
        end else if (en) begin


            //operand and tag pass thru 
            a_out <= a_in;
            b_out <= b_in;
            first_out <= first_in;
            last_out  <= last_in;

            //acc
            if (first_in) acc <= ACC_WIDTH'(product);
            else if (active) acc <= acc + ACC_WIDTH'(product);
            //acc window 
            active <= (first_in | active) & ~last_in;
            //shadow capture and drain 
            cap <= last_in;
            if (cap) drain_out <= acc;
            else if (drain_shift) drain_out <= drain_in;
        end 
    end
endmodule