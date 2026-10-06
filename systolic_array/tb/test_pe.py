""" cocotb tb for rtl/pe.sv """
 
import os
import random
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer
from pe_model import PEModel, expected_dot, signed_range, wrap_signed
from delay_model import expected_out

#helpers

def read_int(sig, name): 
    raw = str(sig.value)
    if "x" in raw.lower() or "z" in raw.lower(): 
        raise AssertionError(f"{name} is not resolvable: {raw}")
    return int(sig.value)

def read_signed(sig, name, width): 
    raw = read_int(sig, name)
    return raw - (1 << width) if raw >= (1 << (width - 1)) else raw 

def to_unsigned(value, width):
    return value & ((1 << width) - 1)


class PEHarness: 
    """Drives the DUT and the golden model from one input vector.
 
    Everything in this file goes through `step()`. That is deliberate: if the
    testbench ever drives the DUT and the model from separately-constructed
    stimulus, a mismatch stops being evidence about the RTL and starts being
    evidence about the testbench.
    """

    PORTS = ("a_out", "b_out", "first_out", "last_out", "drain_out")
    INTERNAL = ("acc", "active") 

    def __init__(self, dut): 
        self.dut = dut 
        self.dw = int(dut.DATA_WIDTH.value) 
        self.aw = int(dut.ACC_WIDTH.value) 
        self.model = PEModel(self.dw, self.aw) 
        self.cycle = 0 
        self.seed = int(os.environ.get("SEED") or random.randrange(2**31))
        self.rng = random.Random(self.seed) 


    async def start(self, reset_cycles = 3): 
        cocotb.start_soon(Clock(self.dut.clk, 10, unit="ns").start())
        self.dut.reset.value = 1 
        self._drive_idle() 
        for _ in range (reset_cycles): 
            await RisingEdge(self.dut.clk) 
            self.model.step(reset=1) 
        await FallingEdge(self.dut.clk)
        self.dut.reset.value = 0 
        self.dut._log.info(f"DUT parameters: DATA_WIDTH={self.dw} ACC_WIDTH={self.aw} "
                           f"SEED= {self.seed} (reproduce with: make MODULE_NAME=pe"
                           f"K=<k> SEED={self.seed})")


    def _drive_idle(self): 
        d = self.dut 
        d.a_in.value = 0 
        d.b_in.value = 0 
        d.first_in.value = 0 
        d.last_in.value = 0 
        d.drain_shift.value = 0 
        d.drain_in.value = 0 
        d.en.value = 1 

    async def step(self, a_in=0, b_in = 0, first_in = 0, last_in=0, drain_shift=0,
                    drain_in=0, en = 1, reset = 0, check = True): 
        """advance 1 rising edge on both dut and model, then compare 
        returns the expected ReadOnly view of the model state"""

        d = self.dut 
        await FallingEdge(d.clk) 
        d.a_in.value = to_unsigned(a_in, self.dw) 
        d.b_in.value = to_unsigned(b_in, self.dw)
        d.first_in.value = first_in
        d.last_in.value = last_in
        d.drain_shift.value = drain_shift
        d.drain_in.value = to_unsigned(drain_in, self.aw)
        d.en.value = en
        d.reset.value = reset
 
        await RisingEdge(d.clk)
        await ReadOnly()


        expected = self.model.step(a_in=a_in, b_in=b_in, first_in=first_in, last_in=last_in,
                                   drain_shift=drain_shift, drain_in=drain_in, en=en, reset=reset)
        if check:
            self._compare(expected, a_in, b_in, first_in, last_in, en, reset)
            self.cycle += 1 
            return expected 


    def _compare(self, expected, a_in, b_in, first_in, last_in, en, reset) : 
        d = self.dut 
        widths = { 
            "a_out": self.dw, "b_out": self.dw, "first_out": 1, "last_out" : 1, 
            "drain_out" : self.aw, "acc": self.aw, "active" : 1
        }
        for name in self.PORTS + self.INTERNAL: 
            w = widths[name] 
            sig = getattr(d, name) 
            got = read_signed(sig, name, w) if w > 1 else read_int(sig, name) 
            exp = getattr(expected, name )
            assert got == exp, (  
                f"cycle {self.cycle}: {name}={got}, expected {exp}\n"
                f"stimulus: a_in={a_in} b_in={b_in} first_in={first_in} "
                f"last_in={last_in} en={en} reset={reset}\n"
                f"seed={self.seed}"
            )
    async def run_tile(self, a_vec, b_vec) -> int:
        """Drive one complete K-length tile and return the drained result."""
        if len(a_vec) != len(b_vec):
            raise ValueError("a_vec and b_vec must be the same length")
        k = len(a_vec)
        for i, (a, b) in enumerate(zip(a_vec, b_vec)):
            await self.step(
                a_in=a,
                b_in=b,
                first_in=int(i == 0),
                last_in=int(i == k - 1),
            )
        s = await self.step()      # cap fires here, copying acc into the shadow
        return s.drain_out
    
    def random_operands(self, n): 
        lo, hi = signed_range(self.dw)
        return [self.rng.randint(lo,hi)for _ in range (n)] 

def get_k(): 
    return int(os.environ.get ("K") or 8)


    #signedness 
@cocotb.test() 
async def test_signed_quadrants(dut): 
    """all 4 sign combinations + asymetrical extreme""" 

    h = PEHarness(dut) 
    await h.start() 
    lo, hi = signed_range(h.dw) 
    vectors = [ (7,6) , (7,-6), (-7, 6), (-7,-6), (lo, lo), (lo, hi) ,
                (hi, hi), (lo, 1), (1, lo), (0, lo),
    ]

    for a, b in vectors: 
        result = await h.run_tile ([a], [b])
        assert result == a*b, f"{a} * {b} = {result}, expected {a * b}"
    dut._log.info(f" sign quadrants passed ({len(vectors)} vectors)")

#acc 
@cocotb.test() 
async def test_k_accumulation_random(dut): 
    """randomized k length accumalation against golden model"""
    h = PEHarness(dut)
    await h.start()
    k = get_k()

    for tile in range(8): 
        a_vec = h.random_operands(k) 
        b_vec = h.random_operands(k) 
        result = await h.run_tile(a_vec, b_vec) 
        expected = expected_dot(a_vec, b_vec, h.aw) 
        assert expected == result, (
            f" tile{tile} : K{k} result={result} expected{expected}, " 
            f" seed{h.seed} "
        )
        await h.step() 
    dut._log.info(f"random accumalation passed: 8 tiles at K={k}")


@cocotb.test() 
async def test_k1_coincdent_irst_last(dut): 
    """first_in and last_in are asserted on the same cycle"""
    h = PEHarness(dut) 
    await h.start() 
    await h.step(a_in=-128, b_in=-128, first_in = 1, last_in = 1)
    assert read_int( dut.active, "active") == 0, ( 
        "active must clear when first_in and last_in coincide (k = 1)" 
    )
    s = await h.step() 
    assert s.drain_out == 16384

    for _ in range (5): 
        await h.step(a_in=50, b_in = 50) 
        assert read_signed (dut.acc, "acc", h.aw) == 16384, (
            'acc kept accumalating after k=1 tile ws completed' 
        )
    dut._log.info ("K=1 coincident first/last passed")

@cocotb.test()
async def test_first_clears_not_adds(dut): 
    """back to back tests shouldnt inherit the previous accumalator"""
    h = PEHarness(dut)
    await h.start()

    r1 = await h.run_tile ([10, 10, 10], [10, 10, 10])
    assert r1 == 300 
    r2 = await h.run_tile ([1, 1,1], [1, 1, 1]) 
    assert r2 == 3 , f"2nd tile inherited state from 1st : got {r2}"
    dut._log.info("first_in clears rather than accumulates")


@cocotb.test() 
async def test_acc_frozen_after_last(dut): 
    """acc should freeze the cycle after last_in"""

    h = PEHarness(dut) 
    await h.start() 
    k = get_k() 
    a_vec = h.random_operands(k)
    b_vec = h.random_operands(k)
    expected = expected_dot(a_vec, b_vec, h.aw)

    for i in range(k): 
        await h.step(a_in =  a_vec[i], b_in = b_vec[i], first_in=int(i == 0), last_in=int(i == k - 1))
    lo, _ = signed_range(h.dw) 
    for _ in range(10): 
        await h.step(a_in = lo, b_in = lo)  
        assert read_signed (dut.acc, "acc", h.aw) == expected, (
            "acc moved after last_in; the acc window isnt closing" )
    dut._log.info("acc correct frozen after last_in properly")

#pass thru, reusing golden model from delay 

@cocotb.test()
async def test_passthrough_matches_delay_depth1(dut):
    """The operand path is observationally a delay with DEPTH=1.
    """
    h = PEHarness(dut)
    await h.start()
 
    n = 24
    a_hist, b_hist = [], []
    for i in range(n):
        a, b = h.rng.randint(1, 127), h.rng.randint(1, 127)
        a_hist.append(a)
        b_hist.append(b)
        await h.step(a_in=a, b_in=b)
        for sig, hist, name in ((dut.a_out, a_hist, "a_out"),
                                (dut.b_out, b_hist, "b_out")):
            got = read_signed(sig, name, h.dw)
            exp = expected_out(hist, i, 1)
            assert got == exp, (
                f"cycle {i}: {name}={got}, expected {exp} "
                f"(delay model, DEPTH=1)"
            )
 
    dut._log.info("operand pass thru matches delay DEPTH=1")
 
 
@cocotb.test()
async def test_no_combinational_path(dut):
    """No input may reach any output within the same cycle."""
    h = PEHarness(dut)
    await h.start()
 
    await FallingEdge(dut.clk)
    dut.a_in.value = to_unsigned(0x3C, h.dw)
    dut.b_in.value = to_unsigned(0x3C, h.dw)
    await Timer(1, unit="ns")
    await ReadOnly()
    assert read_signed(dut.a_out, "a_out", h.dw) == 0, (
        "a_out tracked a_in combinationally; the PE must be fully registered"
    )
    dut._log.info("no combinational input-to-output path")
 
@cocotb.test()
async def test_drain_capture_and_shift(dut):
    """cap timing, shift timing, and capture priority over shift."""
    h = PEHarness(dut)
    await h.start()
 
    # cap fires exactly one cycle after last_in, not on it.
    await h.step(a_in=6, b_in=7, first_in=1, last_in=1)
    assert read_signed(dut.drain_out, "drain_out", h.aw) != 42, (
        "drain_out captured on the last_in cycle; capture must be one cycle later"
    )
    s = await h.step()
    assert s.drain_out == 42, "cap did not fire one cycle after last_in"
 
    # drain_shift moves drain_in with DEPTH=1 timing.
    s = await h.step(drain_shift=1, drain_in=12345)
    assert s.drain_out == 12345
    s = await h.step(drain_shift=1, drain_in=-9999)
    assert s.drain_out == -9999
 
    # Held when neither cap nor shift is asserted.
    s = await h.step()
    assert s.drain_out == -9999, "shadow register did not hold"
 
    # Capture wins over a coincident shift.
    await h.step(a_in=2, b_in=3, first_in=1, last_in=1)
    s = await h.step(drain_shift=1, drain_in=777)
    assert s.drain_out == 6, (
        f"shift won over capture on a coincident cycle: got {s.drain_out}"
    )
 
    dut._log.info("drain capture and shift timing passed")
  
@cocotb.test()
async def test_reset_mid_accumulation(dut):
    """Reset mid-tile clears acc, active and drain_out."""
    h = PEHarness(dut)
    await h.start()
    k = max(get_k(), 3)
 
    a_vec = h.random_operands(k)
    b_vec = h.random_operands(k)
    for i in range(k // 2):
        await h.step(a_in=a_vec[i], b_in=b_vec[i], first_in=int(i == 0))
 
    await h.step(reset=1)
    assert read_signed(dut.acc, "acc", h.aw) == 0, "acc not cleared by reset"
    assert read_int(dut.active, "active") == 0, "active not cleared by reset"
    assert read_signed(dut.drain_out, "drain_out", h.aw) == 0
 
    # Reset must also win while the array is stalled, or a stalled design cannot be recovered.
    await h.step(a_in=9, b_in=9, first_in=1)
    await h.step(a_in=9, b_in=9, en=0, reset=1)
    assert read_signed(dut.acc, "acc", h.aw) == 0, (
        "reset did not take priority over en; a stalled PE cannot be reset"
    )
 
    # And the PE still works afterwards.
    result = await h.run_tile([4, 5], [6, 7])
    assert result == 59, f"PE broken after reset: got {result}"
 
    dut._log.info("reset mid-accumulation passed")
 
 
@cocotb.test()
async def test_en_holds_without_loss(dut):
    """en low holds every register with no lost or duplicated products."""
    h = PEHarness(dut)
    await h.start()
    k = max(get_k(), 4)
 
    a_vec = h.random_operands(k)
    b_vec = h.random_operands(k)
    expected = expected_dot(a_vec, b_vec, h.aw)
 
    for i in range(k):
        await h.step(a_in=a_vec[i], b_in=b_vec[i],
                     first_in=int(i == 0), last_in=int(i == k - 1))
        # Stall for a random number of cycles mid-tile, driving garbage that must be ignored entirely.
        for _ in range(h.rng.randint(0, 3)):
            await h.step(a_in=h.rng.randint(-128, 127),
                         b_in=h.rng.randint(-128, 127), en=0)
 
    s = await h.step()
    assert s.drain_out == expected, (
        f"stalling changed the result: got {s.drain_out}, expected {expected}, "
        f"seed={h.seed}"
    )
 
    dut._log.info("en stall holds state with no product loss")
 
 #accumalator width 
@cocotb.test()
async def test_accumulator_width(dut):
    """Near-overflow at the configured width, and wrapping past it.
    """
    h = PEHarness(dut)
    await h.start()
    k = get_k()
    lo, _ = signed_range(h.dw)
 
    # K copies of the maximum-magnitude product.
    a_vec = [lo] * k
    b_vec = [lo] * k
    result = await h.run_tile(a_vec, b_vec)
    raw = k * (lo * lo)
    expected = wrap_signed(raw, h.aw)
    assert result == expected, (
        f"K={k} max-magnitude accumulation: got {result}, expected {expected}"
    )
 
    if raw != expected:
        dut._log.info(
            f"wrap exercised: raw={raw} wrapped to {expected} "
            f"at ACC_WIDTH={h.aw} (saturation would have given a different value)"
        )
    else:
        dut._log.info(
            f"no overflow at ACC_WIDTH={h.aw}, K={k} (headroom confirmed)"
        )
