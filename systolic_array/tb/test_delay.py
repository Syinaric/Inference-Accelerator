#cocotb based hardware verification script 
import os 
import random

import cocotb 
from cocotb.clock import Clock 
from cocotb.triggers import FallingEdge, ReadOnly, RisingEdge, Timer
from delay_model import expected_out 




#helper functions
def read_int(sig, name): 
    #help find fails 
    raw = str(sig.value)
    if "x" in raw.lower() or 'x' in raw.lower(): 
        raise AssertionError(f"{name} is not resolvable: {raw}")
    return int(sig.value)


async def start_clock(dut): 
    cocotb.start_soon(Clock(dut.clk, 10, unit = "ns").start())

async def apply_reset(dut, cycles = 3): 
    dut.reset.value = 1
    # en is new: the skew chains now stall with the PE grid. These tests never
    # exercise stalling, so hold it high and the module behaves exactly as before.
    dut.en.value = 1
    dut.in_data.value = 0 
    for _ in range(cycles): 
        await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.reset.value = 0


def make_stimulus(n, width, rng): 
    #removes hardcoded list [10,20,30,40,40,60,70,80].   This makes a better test
    hi = (1<<width) - 1 
    assert n <= hi, f"cannot draw {n} distinct nonzero {width} - bit values " 
    pool = rng.sample(range(1, hi + 1), n) 
    pool[0] = hi 
    return pool 








#TESTS 


@cocotb.test()
#main streaming test. drives randon data and tests against model 
async def test_delay_module(dut): 

    await start_clock(dut) 

    seed = int(os.environ.get("SEED") or random.randrange(2**31))
    rng = random.Random(seed)
    dut._log.info(f"SEED={seed}.    (reprocude with: make DEPTH=<d> SEED = {seed})")

    await apply_reset(dut) 
    depth = int(dut.DEPTH.value) 
    width = int(dut.DATA_WIDTH.value) #not hard coded anymore 
    dut._log.info(f"DUT parameters: DEPTH= {depth} DATA_WIDTH= :{width}")
    n = max(4 * depth, 24)
    stimulus = make_stimulus(n, width, rng)

    history = [] 

    for i in range(n): 
        await FallingEdge(dut.clk) 
        dut.in_data.value = stimulus[i] 
        history.append(stimulus[i])

        await RisingEdge(dut.clk)
        await ReadOnly ()
        observed = read_int(dut.out_data, "out_data") 
        expected = expected_out(history, i, depth) 
        dut._log.debug(f"cycle{i:3d}: in= {stimulus[i]:3d} out = {observed:3d} expected = {expected:3d}")
        assert observed == expected, (
            f"cycle {i}: out_data={observed}, expected {expected} "
            f"(DEPTH={depth}, seed={seed})"
        )
    dut._log.info(f"stream test passed: {n} cycles at DEPTH={depth}")

@cocotb.test()
async def test_reset_behaviour(dut): 
    #checks reset behaviour and does it mid stream
    await start_clock(dut) 
    await apply_reset(dut) 

    depth = int(dut.DEPTH.value)
    width = int(dut.DATA_WIDTH.value)
    if depth == 0: 
        dut._log.info("DEPTH=0: bypass path has no state; reset check N/A")
        return

    # fill pipline with known values 
    for _ in range (depth + 2): 
        await FallingEdge(dut.clk) 
        dut.in_data.value = 0xA5 & ((1 << width) - 1)
        await RisingEdge(dut.clk)

    await ReadOnly()
    assert read_int(dut.out_data, "out_data") != 0, "pipeline should be full of nonzero data"

    await FallingEdge(dut.clk) 
    dut.reset.value = 1 
    dut.in_data.value = 0x5A & ((1 << width) - 1)

    await RisingEdge(dut.clk) 
    await ReadOnly() 
    assert read_int(dut.out_data, "out_data") == 0, "output not cleared on reset edge"

    for _ in range(3):
        await FallingEdge(dut.clk)
        dut.in_data.value = 0x5A & ((1 << width) - 1)
        await RisingEdge(dut.clk)
        await ReadOnly()
        assert read_int(dut.out_data, "out_data") == 0, "output not held clear during reset"

    rng = random.Random(0xC0FFEE)
    n = 3 * depth + 4
    stimulus = make_stimulus(n, width, rng)
    history = []
    for i in range(n):
        await FallingEdge(dut.clk)
        if i == 0:
            dut.reset.value = 0     # deassert alongside the first real data
        dut.in_data.value = stimulus[i]
        history.append(stimulus[i])
        await RisingEdge(dut.clk)
        await ReadOnly()
        observed = read_int(dut.out_data, "out_data")
        expected = expected_out(history, i, depth)
        assert observed == expected, (
            f"post-reset refill cycle {i}: got {observed}, expected {expected}"
        )

    dut._log.info("reset behaviour test passed")

#this test looks within the capture edge since in other places at the readonly sample point depth=1 and depth = 0
#both have the same output and u cant tell them apart 
@cocotb.test()
async def test_bypass_is_combinational(dut):
    await start_clock(dut)
    await apply_reset(dut)

    depth = int(dut.DEPTH.value)
    width = int(dut.DATA_WIDTH.value)
    probe = 0x3C & ((1 << width) - 1)

    await FallingEdge(dut.clk)
    dut.in_data.value = probe
    await Timer(1, unit="ns")     
    await ReadOnly()
    observed = read_int(dut.out_data, "out_data")

    if depth == 0:
        assert observed == probe, (
            f"DEPTH=0 must be a combinational bypass: out={observed}, in={probe}"
        )
        dut._log.info("bypass path confirmed combinational")
    else:
        # Pipeline is still all zeros from reset, so the output must not have
        # tracked the input within this cycle.
        assert observed == 0, (
            f"DEPTH={depth} must be registered, but out_data tracked in_data "
            f"combinationally (out={observed})"
        )
        dut._log.info(f"DEPTH={depth} confirmed registered (no combinational path)")