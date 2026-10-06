// Purpose: stagger (skew) the matrix inputs.

// CHANGE: added a global `en` stall input so this block can sit in front of the
// PE grid. pe.sv gates every register on `en`; if the skew chains keep shifting
// while the PEs are frozen, operands slide out of alignment and never recover.
// Both must stall together or neither can.
module delay #(
    parameter int DATA_WIDTH = 8,
    parameter int DEPTH = 1
)(
    input  logic clk,
    input  logic reset,
    input  logic en,
    input  logic [DATA_WIDTH-1:0] in_data,
    output logic [DATA_WIDTH-1:0] out_data
);

    generate
        if (DEPTH == 0) begin : bypass_logic
            assign out_data = in_data;
        end
        else begin : shift_reg_logic
            logic [DATA_WIDTH-1:0] shift_reg [0:DEPTH-1];

            always_ff @(posedge clk) begin
                if (reset) begin
                    for (int i = 0; i < DEPTH; i++) begin
                        shift_reg[i] <= '0;
                    end
                end else if (en) begin
                    shift_reg[0] <= in_data;
                    for (int i = 1; i < DEPTH; i++) begin
                        shift_reg[i] <= shift_reg[i-1];
                    end
                end
            end

            assign out_data = shift_reg[DEPTH-1];
        end
    endgenerate

endmodule