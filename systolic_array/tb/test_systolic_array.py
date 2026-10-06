""" cocotb tb for rtl/systolic_array.sv """

import os
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge

from systolic_array_model import SystolicArrayModel, expected_matmul
from pe_model import signed_range


def to_unsigned(value, width):
    return value & ((1 << width) - 1)


def to_signed(value, width):
    value &= (1 << width) - 1
    if value >= (1 << (width - 1)):
        value -= 1 << width
    return value


def read_int(sig, name):
    raw = str(sig.value)
    if "x" in raw.lower() or "z" in raw.lower():
        raise AssertionError(f"{name} is not resolvable: {raw}")
    return int(sig.value)


class ArrayHarness:
    """Drives the DUT and the mesh model from one input vector.

    Same discipline as PEHarness: every cycle goes through step(), so the DUT
    and the model can never diverge because of separately built stimulus.
    """

    def __init__(self, dut):
        self.dut = dut
        self.n = int(dut.N.value)
        self.dw = int(dut.DATA_WIDTH.value)
        self.aw = int(dut.ACC_WIDTH.value)
        self.model = SystolicArrayModel(self.n, self.dw, self.aw)
        self.cycle = 0
        self.seed = int(os.environ.get("SEED") or random.randrange(2**31))
        self.rng = random.Random(self.seed)

    #bus packing 
    # a_in[r*DW +: DW] is row r, b_in[c*DW +: DW] is column c,
    # c_out[c*AW +: AW] is the bottom of column c.

    def _pack(self, values, width):
        word = 0
        for i, v in enumerate(values):
            word |= to_unsigned(v, width) << (i * width)
        return word

    def _unpack_c_out(self):
        word = read_int(self.dut.c_out, "c_out")
        mask = (1 << self.aw) - 1
        return [to_signed((word >> (c * self.aw)) & mask, self.aw)
                for c in range(self.n)]

    #lifecycle

    async def start(self, reset_cycles=3):
        cocotb.start_soon(Clock(self.dut.clk, 10, unit="ns").start())
        self.dut.reset.value = 1
        self._drive_idle()
        for _ in range(reset_cycles):
            await RisingEdge(self.dut.clk)
            self.model.step(reset=1)
        await FallingEdge(self.dut.clk)
        self.dut.reset.value = 0
        self.dut._log.info(
            f"DUT parameters: N={self.n} DATA_WIDTH={self.dw} ACC_WIDTH={self.aw} "
            f"K={get_k()} SEED={self.seed} (reproduce with: "
            f"make MODULE_NAME=systolic_array N={self.n} K={get_k()} SEED={self.seed})"
        )

    def _drive_idle(self):
        d = self.dut
        d.a_in.value = 0
        d.b_in.value = 0
        d.first_in.value = 0
        d.last_in.value = 0
        d.drain_shift.value = 0
        d.en.value = 1

    async def step(self, a_in=None, b_in=None, first_in=0, last_in=0,
                   drain_shift=0, en=1, reset=0, check=True):
        """Advance one rising edge on both DUT and model, then compare."""
        n = self.n
        a_in = [0] * n if a_in is None else list(a_in)
        b_in = [0] * n if b_in is None else list(b_in)

        d = self.dut
        await FallingEdge(d.clk)
        d.a_in.value = self._pack(a_in, self.dw)
        d.b_in.value = self._pack(b_in, self.dw)
        d.first_in.value = first_in
        d.last_in.value = last_in
        d.drain_shift.value = drain_shift
        d.en.value = en
        d.reset.value = reset

        await RisingEdge(d.clk)
        await ReadOnly()

        expected = self.model.step(a_in=a_in, b_in=b_in, first_in=first_in,
                                   last_in=last_in, drain_shift=drain_shift,
                                   en=en, reset=reset)
        if check:
            got = self._unpack_c_out()
            assert got == expected.c_out, (
                f"cycle {self.cycle}: c_out={got}, expected {expected.c_out}\n"
                f"stimulus: first_in={first_in} last_in={last_in} "
                f"drain_shift={drain_shift} en={en} reset={reset}\n"
                f"N={self.n} K={get_k()} seed={self.seed}"
            )
        self.cycle += 1
        return expected

    #tile driving

    async def stream_operands(self, a_mat, b_mat, en_pattern=None):
        """Push K columns of A and K rows of B through the array boundary.

        en_pattern, if given, is a list of booleans consumed one per attempted
        cycle; a False inserts a stall cycle that re-presents nothing and must
        leave the result unchanged.
        """
        n, k = self.n, len(a_mat[0])
        pattern = list(en_pattern) if en_pattern else []
        pi = 0
        for j in range(k):
            while pi < len(pattern) and not pattern[pi]:
                await self.step(en=0)
                pi += 1
            if pi < len(pattern):
                pi += 1
            await self.step(
                a_in=[a_mat[r][j] for r in range(n)],
                b_in=[b_mat[j][c] for c in range(n)],
                first_in=int(j == 0),
                last_in=int(j == k - 1),
            )

    async def drain(self):
        """Wait out the capture wavefront, then shift the columns out.

        Returns C as an N x N list. Row N-1 appears without any shifting;
        each drain_shift cycle brings up the next row northward.
        """
        n = self.n
        for _ in range(self.model.last_to_drain_ready):
            await self.step()
        c = [[0] * n for _ in range(n)]
        c[n - 1] = self._unpack_c_out()
        for i in range(n - 1):
            await self.step(drain_shift=1)
            c[n - 2 - i] = self._unpack_c_out()
        return c

    async def run_tile(self, a_mat, b_mat, en_pattern=None):
        await self.stream_operands(a_mat, b_mat, en_pattern)
        return await self.drain()

    def random_matrices(self, k):
        lo, hi = signed_range(self.dw)
        a = [[self.rng.randint(lo, hi) for _ in range(k)] for _ in range(self.n)]
        b = [[self.rng.randint(lo, hi) for _ in range(self.n)] for _ in range(k)]
        return a, b


def get_k():
    return int(os.environ.get("K") or 8)


#  TESTS


@cocotb.test()
async def test_random_tile(dut):
    """A full random tile, checked cycle by cycle and end to end.

    step() already compares c_out against the mesh model every cycle. The
    assert below is the independent check: expected_matmul knows nothing about
    skew, drain order or PE state, so it catches the case where model and RTL
    share a misunderstanding.
    """
    h = ArrayHarness(dut)
    await h.start()
    k = get_k()
    a, b = h.random_matrices(k)
    got = await h.run_tile(a, b)
    exp = expected_matmul(a, b, h.aw)
    assert got == exp, f"got {got}\nexp {exp}\nseed={h.seed}"


@cocotb.test()
async def test_drain_latency_boundary(dut):
    """Pin LAST_TO_DRAIN_READY to an exact cycle, from both sides.

    ctrl_fsm.sv will key its drain timing off this constant, so an off-by-one
    here is a silent corruption there. The far corner PE(N-1,N-1) is the last
    to capture, so column N-1 is the one that moves.
    """
    h = ArrayHarness(dut)
    await h.start()
    k = get_k()
    n = h.n
    a, b = h.random_matrices(k)
    exp = expected_matmul(a, b, h.aw)
    target = exp[n - 1][n - 1]

    await h.stream_operands(a, b)

    # One cycle early: the far corner must NOT be there yet. A zero target
    # would make this vacuous, so only assert when the value is distinguishable.
    for _ in range(h.model.last_to_drain_ready - 1):
        await h.step()
    early = h._unpack_c_out()[n - 1]
    if target != 0:
        assert early != target, (
            f"column {n-1} already held {target} at cycle "
            f"{h.model.last_to_drain_ready - 1}; LAST_TO_DRAIN_READY is too "
            f"conservative and the array drains earlier than documented"
        )

    await h.step()
    late = h._unpack_c_out()[n - 1]
    assert late == target, (
        f"column {n-1} was {late}, expected {target} at exactly "
        f"LAST_TO_DRAIN_READY={h.model.last_to_drain_ready} cycles after "
        f"last_in. seed={h.seed}"
    )


@cocotb.test()
async def test_k1_coincident_first_last(dut):
    """K=1 drives first_in and last_in on the same cycle.

    At PE level this exposed the active-flag priority chain. At array level the
    extra risk is the skew network: one coincident tag pair has to arrive at
    N*N PEs at N*N different times without smearing.
    """
    h = ArrayHarness(dut)
    await h.start()
    n = h.n
    a, b = h.random_matrices(1)
    got = await h.run_tile(a, b)
    assert got == expected_matmul(a, b, h.aw), f"seed={h.seed}"


@cocotb.test()
async def test_en_stall_is_transparent(dut):
    """Random stalls must not change the result by even one LSB.

    This is the test that exercises the `en` port added to delay.sv. Before
    that change the skew chains kept shifting while the PE grid was frozen,
    which permanently misaligns operands. Run the same tile twice, once clean
    and once with stalls injected, and demand identical results.
    """
    h = ArrayHarness(dut)
    await h.start()
    k = get_k()
    a, b = h.random_matrices(k)
    clean = await h.run_tile(a, b)

    # A fresh reset, then the same operands with stalls sprinkled in.
    for _ in range(3):
        await h.step(reset=1)
    pattern = [h.rng.random() > 0.4 for _ in range(k * 3)]
    stalled = await h.run_tile(a, b, en_pattern=pattern)

    assert stalled == clean, (
        f"stalling changed the result\nclean   {clean}\nstalled {stalled}\n"
        f"seed={h.seed}"
    )
    assert clean == expected_matmul(a, b, h.aw), f"seed={h.seed}"


@cocotb.test()
async def test_en_stall_during_drain(dut):
    """en low mid-drain holds the chain rather than dropping results.

    pe.sv puts drain_out under `else if (en)`, so a stall during readout must
    freeze the shift chain, not skip a row.
    """
    h = ArrayHarness(dut)
    await h.start()
    n = h.n
    a, b = h.random_matrices(get_k())
    exp = expected_matmul(a, b, h.aw)

    await h.stream_operands(a, b)
    for _ in range(h.model.last_to_drain_ready):
        await h.step()

    got = [[0] * n for _ in range(n)]
    got[n - 1] = h._unpack_c_out()
    for i in range(n - 1):
        # Stall twice with drain_shift asserted; the chain must not advance.
        held = h._unpack_c_out()
        await h.step(drain_shift=1, en=0)
        assert h._unpack_c_out() == held, "drain chain advanced while en was low"
        await h.step(drain_shift=1, en=0)
        await h.step(drain_shift=1)
        got[n - 2 - i] = h._unpack_c_out()

    assert got == exp, f"got {got}\nexp {exp}\nseed={h.seed}"


@cocotb.test()
async def test_reset_mid_tile(dut):
    """Reset partway through a tile clears the array and the next tile is clean."""
    h = ArrayHarness(dut)
    await h.start()
    k = get_k()
    a, b = h.random_matrices(k)

    # Get partway in, then yank reset.
    for j in range(min(k, max(1, k // 2))):
        await h.step(
            a_in=[a[r][j] for r in range(h.n)],
            b_in=[b[j][c] for c in range(h.n)],
            first_in=int(j == 0),
        )
    for _ in range(3):
        await h.step(reset=1)
    assert h._unpack_c_out() == [0] * h.n, "c_out not cleared by reset"

    a2, b2 = h.random_matrices(k)
    got = await h.run_tile(a2, b2)
    assert got == expected_matmul(a2, b2, h.aw), (
        f"tile after reset was corrupted by pre-reset state, seed={h.seed}"
    )


@cocotb.test()
async def test_signed_extremes(dut):
    """Every operand at the signed floor, the worst case for accumulator width.

    -128 * -128 = +16384 is the largest magnitude product, and it is positive,
    which is the asymmetry that catches sign-extension mistakes.
    """
    h = ArrayHarness(dut)
    await h.start()
    k = get_k()
    lo, hi = signed_range(h.dw)
    for va, vb in [(lo, lo), (lo, hi), (hi, lo), (hi, hi)]:
        for _ in range(3):
            await h.step(reset=1)
        a = [[va] * k for _ in range(h.n)]
        b = [[vb] * h.n for _ in range(k)]
        got = await h.run_tile(a, b)
        assert got == expected_matmul(a, b, h.aw), (
            f"a={va} b={vb} got {got} exp {expected_matmul(a, b, h.aw)}"
        )


@cocotb.test()
async def test_back_to_back_tiles_no_leakage(dut):
    """Three tiles in a row without reset. Nothing may carry over.

    first_in clearing the accumulator rather than adding to it is what makes
    this work; if that priority were ever inverted, tile 2 would silently be
    tile 1 plus tile 2.
    """
    h = ArrayHarness(dut)
    await h.start()
    k = get_k()
    for tile in range(3):
        a, b = h.random_matrices(k)
        got = await h.run_tile(a, b)
        assert got == expected_matmul(a, b, h.aw), (
            f"tile {tile} wrong, seed={h.seed}"
        )


@cocotb.test()
async def test_drain_chain_flushes_to_zero(dut):
    """Over-shifting past N must produce zeros, not stale or wrapped data.

    The top of every drain chain is tied to zero in the RTL. If a nonzero value
    reappears after N shifts, the chain is longer than N or wired in a loop.
    """
    h = ArrayHarness(dut)
    await h.start()
    a, b = h.random_matrices(get_k())
    await h.run_tile(a, b)
    for _ in range(h.n):
        await h.step(drain_shift=1)
    assert h._unpack_c_out() == [0] * h.n, (
        "drain chain did not flush to zero after over-shifting"
    )