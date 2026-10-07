 // requant: Converts a 32-bit signed accumulator from
// the array drain into a signed INT8 activation for the next layer.

module requant #(
    parameter int ACC_WIDTH = 32,
    parameter int BIAS_WIDTH = 32,
    parameter int SAT_WIDTH = 32,   // width the (acc + bias) sum is saturated to
    parameter int M_WIDTH = 32,   // unsigned multiplier width
    parameter int SHIFT_WIDTH = 6,    // total right shift, 0 .. 2**SHIFT_WIDTH-1
    parameter int DATA_WIDTH = 8,    // output width (INT8)
    parameter int TAG_WIDTH = 4     // sideband carried with the data (e.g. c_row)
)(
    input  logic clk,
    input  logic reset,      // synchronous
    input  logic en,

    // data in, from the array drain
    input  logic in_valid,
    input  logic signed [ACC_WIDTH-1:0]   in_acc,
    input  logic [TAG_WIDTH-1:0] in_tag,

    // per-channel quantization parameters, sampled with in_acc
    input  logic signed [BIAS_WIDTH-1:0]  bias,
    input  logic [M_WIDTH-1:0] mult,
    input  logic [SHIFT_WIDTH-1:0] shift,
    input  logic signed [DATA_WIDTH-1:0] zero_point,
    input  logic relu_en,

    // data out, LATENCY enabled edges later
    output logic out_valid,
    output logic signed [DATA_WIDTH-1:0] out_q,
    output logic [TAG_WIDTH-1:0] out_tag
);
    // Widths
    localparam int LATENCY  = 4;

    // acc + bias needs one bit more than the wider operand
    localparam int SUM_FULL_W = ((ACC_WIDTH > BIAS_WIDTH) ? ACC_WIDTH : BIAS_WIDTH) + 1;
    // signed SAT_WIDTH x unsigned M_WIDTH (zero-extended to M_WIDTH+1 signed)
    localparam int P_W = SAT_WIDTH + M_WIDTH + 1;
    // one extra bit so adding the zero point can never wrap before the clamp
    localparam int Q_W = P_W + 1;

    initial begin
        if (SAT_WIDTH > SUM_FULL_W)
            $fatal(1, "requant: SAT_WIDTH=%0d exceeds the natural sum width %0d",
                   SAT_WIDTH, SUM_FULL_W);
        if (SAT_WIDTH < 2 || M_WIDTH < 1 || DATA_WIDTH < 2)
            $fatal(1, "requant: degenerate width parameter");
        // the rounding constant 2^(shift-1) plus |prod| < 2^(P_W-2) must not
        // overflow P_W signed bits
        if ((2**SHIFT_WIDTH) - 1 > P_W - 2)
            $fatal(1, "requant: SHIFT_WIDTH=%0d allows shifts beyond the product width %0d",
                   SHIFT_WIDTH, P_W);
    end

    // Saturation bounds, built by replication rather than 2**N so they are
    // exact at 32 bits and wider (2**31 overflows a 32-bit int).
    localparam logic signed [SUM_FULL_W-1:0] SAT_MAX =
        {{(SUM_FULL_W-SAT_WIDTH+1){1'b0}}, {(SAT_WIDTH-1){1'b1}}};
    localparam logic signed [SUM_FULL_W-1:0] SAT_MIN =
        {{(SUM_FULL_W-SAT_WIDTH+1){1'b1}}, {(SAT_WIDTH-1){1'b0}}};

    localparam logic signed [Q_W-1:0] OUT_MAX =
        {{(Q_W-DATA_WIDTH+1){1'b0}}, {(DATA_WIDTH-1){1'b1}}};
    localparam logic signed [Q_W-1:0] OUT_MIN =
        {{(Q_W-DATA_WIDTH+1){1'b1}}, {(DATA_WIDTH-1){1'b0}}};

    // Stage 1: bias add + saturate
    logic signed [SUM_FULL_W-1:0] sum_full;
    logic signed [SAT_WIDTH-1:0]  sum_sat;

    // size casts of signed operands sign-extend
    assign sum_full = SUM_FULL_W'(in_acc) + SUM_FULL_W'(bias);

    always_comb begin
        if (sum_full > SAT_MAX) sum_sat = SAT_MAX[SAT_WIDTH-1:0];
        else if (sum_full < SAT_MIN) sum_sat = SAT_MIN[SAT_WIDTH-1:0];
        else sum_sat = sum_full[SAT_WIDTH-1:0];
    end

    logic s1_valid;
    logic signed [SAT_WIDTH-1:0] s1_sum;
    logic [M_WIDTH-1:0] s1_mult;
    logic [SHIFT_WIDTH-1:0] s1_shift;
    logic signed [DATA_WIDTH-1:0] s1_zp;
    logic s1_relu;
    logic [TAG_WIDTH-1:0] s1_tag;

    always_ff @(posedge clk) begin
        if (reset) begin
            s1_valid <= 1'b0;
            s1_sum <= '0;
            s1_mult <= '0;
            s1_shift <= '0;
            s1_zp <= '0;
            s1_relu <= 1'b0;
            s1_tag <= '0;
        end else if (en) begin
            s1_valid <= in_valid;
            s1_sum <= sum_sat;
            s1_mult <= mult;
            s1_shift <= shift;
            s1_zp <= zero_point;
            s1_relu <= relu_en;
            s1_tag <= in_tag;
        end
    end
    // Stage 2: multiply (registered on its own so Vivado can map it onto DSPs)
    logic signed [P_W-1:0] prod;

    // mult is unsigned: prepend a 0 so the signed multiply treats it as positive
    assign prod = P_W'(s1_sum) * P_W'($signed({1'b0, s1_mult}));

    logic s2_valid;
    logic signed [P_W-1:0] s2_prod;
    logic [SHIFT_WIDTH-1:0] s2_shift;
    logic signed [DATA_WIDTH-1:0] s2_zp;
    logic s2_relu;
    logic [TAG_WIDTH-1:0] s2_tag;

    always_ff @(posedge clk) begin
        if (reset) begin
            s2_valid <= 1'b0;
            s2_prod <= '0;
            s2_shift <= '0;
            s2_zp <= '0;
            s2_relu <= 1'b0;
            s2_tag <= '0;
        end else if (en) begin
            s2_valid <= s1_valid;
            s2_prod <= prod;
            s2_shift <= s1_shift;
            s2_zp <= s1_zp;
            s2_relu <= s1_relu;
            s2_tag <= s1_tag;
        end
    end
    // Stage 3: round half up, arithmetic right shift
    //   floor((x + 2^(s-1)) / 2^s) for s > 0, x unchanged for s == 0
    logic signed [P_W-1:0] rnd;
    logic signed [P_W-1:0] biased;
    logic signed [P_W-1:0] scaled;

    assign rnd = (s2_shift == '0) ? '0 : (P_W'(1) <<< (s2_shift - 1'b1));
    assign biased = s2_prod + rnd;
    assign scaled = biased >>> s2_shift;   // >>> on a signed operand = floor division

    logic s3_valid;
    logic signed [P_W-1:0] s3_scaled;
    logic signed [DATA_WIDTH-1:0] s3_zp;
    logic s3_relu;
    logic [TAG_WIDTH-1:0] s3_tag;

    always_ff @(posedge clk) begin
        if (reset) begin
            s3_valid <= 1'b0;
            s3_scaled <= '0;
            s3_zp <= '0;
            s3_relu <= 1'b0;
            s3_tag <= '0;
        end else if (en) begin
            s3_valid <= s2_valid;
            s3_scaled <= scaled;
            s3_zp <= s2_zp;
            s3_relu <= s2_relu;
            s3_tag <= s2_tag;
        end
    end
    // Stage 4: add output zero point, clamp to INT8 (ReLU raises the floor
    // to the zero point, which is where real 0.0 lands)
    logic signed [Q_W-1:0] with_zp;
    logic signed [Q_W-1:0] lo;
    logic signed [DATA_WIDTH-1:0] q_next;

    assign with_zp = Q_W'(s3_scaled) + Q_W'(s3_zp);
    assign lo = s3_relu ? Q_W'(s3_zp) : OUT_MIN;

    always_comb begin
        if (with_zp > OUT_MAX) q_next = OUT_MAX[DATA_WIDTH-1:0];
        else if (with_zp < lo) q_next = lo[DATA_WIDTH-1:0];
        else q_next = with_zp[DATA_WIDTH-1:0];
    end

    always_ff @(posedge clk) begin
        if (reset) begin
            out_valid <= 1'b0;
            out_q <= '0;
            out_tag <= '0;
        end else if (en) begin
            out_valid <= s3_valid;
            out_q <= q_next;
            out_tag <= s3_tag;
        end
    end

endmodule