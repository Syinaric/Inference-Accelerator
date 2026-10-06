// N x N output-stationary systolic array of `pe` cells.
module systolic_array #(
    parameter int N          = 8,
    parameter int DATA_WIDTH = 8,
    parameter int ACC_WIDTH  = 32
)(
    input  logic clk,
    input  logic reset,
    input  logic en,

    input  logic [N*DATA_WIDTH-1:0] a_in,
    input  logic [N*DATA_WIDTH-1:0] b_in,

    // Global tile tags, asserted once at the array boundary. The skew network
    // fans them out to the correct per-row arrival time.
    input  logic first_in,
    input  logic last_in,

    input  logic drain_shift,
    output logic [N*ACC_WIDTH-1:0]  c_out
);

    localparam int LAST_TO_DRAIN_READY = 2*N;
    logic [DATA_WIDTH-1:0] a_h     [0:N-1][0:N  ];  // a_h[r][c] -> PE(r,c).a_in
    logic                  first_h [0:N-1][0:N  ];
    logic                  last_h  [0:N-1][0:N  ];
    logic [DATA_WIDTH-1:0] b_v     [0:N  ][0:N-1];  // b_v[r][c] -> PE(r,c).b_in
    logic [ACC_WIDTH-1:0]  d_v     [0:N  ][0:N-1];  // d_v[r][c] -> PE(r,c).drain_in

    genvar r, c;

    generate
        // west edge
        for (r = 0; r < N; r++) begin : g_row_skew
            delay #(.DATA_WIDTH(DATA_WIDTH), .DEPTH(r+1)) u_a_skew (
                .clk      (clk),
                .reset    (reset),
                .en       (en),
                .in_data  (a_in[r*DATA_WIDTH +: DATA_WIDTH]),
                .out_data (a_h[r][0])
            );
            delay #(.DATA_WIDTH(1), .DEPTH(r+1)) u_first_skew (
                .clk (clk), .reset (reset), .en (en),
                .in_data (first_in), .out_data (first_h[r][0])
            );

            delay #(.DATA_WIDTH(1), .DEPTH(r+1)) u_last_skew (
                .clk (clk), .reset (reset), .en (en),
                .in_data (last_in), .out_data (last_h[r][0])
            );
        end

        // north edge 
        for (c = 0; c < N; c++) begin : g_col_skew
            delay #(.DATA_WIDTH(DATA_WIDTH), .DEPTH(c+1)) u_b_skew (
                .clk      (clk),
                .reset    (reset),
                .en       (en),
                .in_data  (b_in[c*DATA_WIDTH +: DATA_WIDTH]),
                .out_data (b_v[0][c])
            );
            assign d_v[0][c] = '0;
        end

        // the grid
        for (r = 0; r < N; r++) begin : g_pe_row
            for (c = 0; c < N; c++) begin : g_pe_col
                pe #(
                    .DATA_WIDTH (DATA_WIDTH),
                    .ACC_WIDTH  (ACC_WIDTH)
                ) u_pe (
                    .clk         (clk),
                    .reset       (reset),
                    .en          (en),

                    .a_in        (a_h[r][c]),
                    .b_in        (b_v[r][c]),
                    .a_out       (a_h[r][c+1]),   // east
                    .b_out       (b_v[r+1][c]),   // south

                    .first_in    (first_h[r][c]),
                    .last_in     (last_h[r][c]),
                    .first_out   (first_h[r][c+1]),
                    .last_out    (last_h[r][c+1]),

                    .drain_shift (drain_shift),
                    .drain_in    (d_v[r][c]),     // from PE(r-1,c)
                    .drain_out   (d_v[r+1][c])    // to   PE(r+1,c)
                );
            end
        end

        // south edge 
        for (c = 0; c < N; c++) begin : g_c_out
            assign c_out[c*ACC_WIDTH +: ACC_WIDTH] = d_v[N][c];
        end
    endgenerate

endmodule