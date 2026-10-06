#unlike delay model this has to be a state machine rather than a pure function 
#pe golden model 


from typing import NamedTuple

class PEState(NamedTuple):
    """everything visable at the ReadOnly point after 1 rising edge  """
    a_out: int
    b_out: int
    first_out: int
    last_out: int
    drain_out: int
    acc: int
    active: int

def wrap_signed(value: int, width: int) -> int:
    mask = (1 << width) - 1
    v = value & mask
    if v >= (1 << (width - 1)):
        v -= (1 << width)
    return v

def signed_range(width: int):
    """ Min max of signed value of the given width """ 
    return - (1 << (width - 1)) , (1 << (width - 1)) - 1 


class PEModel : 
    """ model of 1 processing element """

    def __init__(self, data_width: int = 8, acc_width: int = 32):
        if acc_width < 2 * data_width:
            raise ValueError(
                f"ACC_WIDTH={acc_width} too narrow for DATA_WIDTH={data_width} "
                f"(need >= {2 * data_width})"
            )
        self.data_width = data_width
        self.acc_width = acc_width
 
        # Register names need to match the RTL exactly so a waveform and a model trace can be read wo translation 
        self.a_out = 0
        self.b_out = 0
        self.first_out = 0
        self.last_out = 0
        self.acc = 0
        self.active = 0
        self.cap = 0
        self.drain_out = 0

    def _check_operand(self, name: str, value: int) -> None: 
        lo, hi = signed_range(self.data_width)
        if not lo <= value <= hi: 
            raise ValueError(f"{name}= {value} outside signed {self.data_width} -bit range"
                             f"[{lo}, {hi}]. this is a stimulus bug, not a DUT")
        
    def state(self) -> PEState: 
        """current registered state """
        return PEState(
            a_out=self.a_out,
            b_out=self.b_out,
            first_out=self.first_out,
            last_out=self.last_out,
            drain_out=self.drain_out,
            acc=self.acc,
            active=self.active,
        )
    def step( self, 
             a_in: int = 0, 
             b_in: int = 0,
             first_in: int = 0, 
             last_in: int = 0, 
             drain_shift: int = 0, 
             drain_in: int = 0, 
             en: int = 1, 
             reset: int = 0,) -> PEState: 
        """ advance 1 rising edge th the given input vector. 
        returns the state t the ReadOnly point after that edge. """
        self._check_operand("a_in", a_in)
        self._check_operand("b_in", b_in) 

        if reset: 
            self.a_out = 0
            self.b_out = 0
            self.first_out = 0
            self.last_out = 0
            self.acc = 0
            self.active = 0
            self.cap = 0
            self.drain_out = 0
            return self.state()
 
        if not en:
            # stall, every register holds
            return self.state()

        old_acc = self.acc 
        old_active = self.active 
        old_cap = self.cap 
        old_drain_out = self.drain_out 
        product = a_in*b_in


        #acc 
        if first_in: 
            new_acc = wrap_signed(product, self.acc_width)
        elif old_active: 
            new_acc = wrap_signed(old_acc + product, self.acc_width) 
        else: 
            new_acc = old_acc #frozen 


        #acc window 
        new_active = int((first_in or old_active) and not last_in)
        #shadow capture and drain 
        new_cap = int(bool(last_in))

        if old_cap: 
            new_drain_out = old_acc
        elif drain_shift: 
            new_drain_out = wrap_signed(drain_in, self.acc_width) 
        else:
            new_drain_out = old_drain_out

        #commit 
        self.a_out = a_in 
        self.b_out = b_in 
        self.first_out = int(bool(first_in))
        self.last_out = int(bool(last_in))
        self.acc = new_acc 
        self.active = new_active 
        self.cap = new_cap 
        self.drain_out = new_drain_out
        return self.state()

    
    def reset_cycles(self, n: int = 2) -> PEState:
       #hold reset for n edges 
        for _ in range(n):
            self.step(reset=1)
        return self.state()

    def run_tile (self, a_vec, b_vec) -> int : 
        """drive 1 k length tile and retuen the drained result. 
        for tests that only care abt the arithmatic 
        """
        if len(a_vec) != len(b_vec):
            raise ValueError ("vectors must be same length ")
        k = len(a_vec) 
        for i , (a, b) in enumerate(zip(a_vec, b_vec)): 
            self.step(
                a_in = a, 
                b_in = b, 
                first_in = int(i == 0 ), 
                last_in = int(i == k-1), 
            )
        self.step()          # cap fires here, copying acc into the shadow
        return self.drain_out

def expected_dot(a_vec, b_vec, acc_width: int = 32) ->int: 
        #Reference dot product with the same wrapping as the acc
    return wrap_signed(sum(a * b for a, b in zip(a_vec, b_vec)), acc_width)


if __name__ == "__main__":
    fails = 0
 
    def chk(name, got, exp):
        global fails
        if got != exp:
            print(f"FAIL {name:<32} got={got} exp={exp}")
            fails += 1
        else:
            print(f"pass {name:<32} = {got}")
 
    pe = PEModel()
    pe.reset_cycles(2)
 
    # K=3: (2*5) + (3*6) + (4*7) = 56
    pe.step(a_in=2, b_in=5, first_in=1)
    pe.step(a_in=3, b_in=6)
    pe.step(a_in=4, b_in=7, last_in=1)
    s = pe.step()
    chk("K=3 accumulate", s.drain_out, 56)
    chk("K=3 active cleared", s.active, 0)
 
    pe.step(a_in=99 - 99, b_in=0)          # keep the pipeline moving
    s = pe.step(a_in=100, b_in=100)         # garbage streaming past
    chk("K=3 acc frozen after last", s.acc, 56)
 
    # K=1 with coincident first/last, at the signed extreme
    s = pe.step(a_in=-128, b_in=-128, first_in=1, last_in=1)
    chk("K=1 active cleared", s.active, 0)
    s = pe.step()
    chk("K=1 signed extreme", s.drain_out, 16384)
    s = pe.step(a_in=50, b_in=50)
    chk("K=1 no runaway accumulate", s.acc, 16384)
 
    # mixed signs: (-7*6) + (5*-4) = -42 + -20 = -62
    pe.step(a_in=-7, b_in=6, first_in=1)
    pe.step(a_in=5, b_in=-4, last_in=1)
    s = pe.step()
    chk("mixed signs", s.drain_out, -62)
 
    # drain chain
    s = pe.step(drain_shift=1, drain_in=12345)
    chk("drain_shift moves drain_in", s.drain_out, 12345)
 
    # en low holds everything
    pe.step(a_in=1, b_in=1, first_in=1)
    pe.step(a_in=100, b_in=100, en=0)
    s = pe.step(a_in=100, b_in=100, en=0)
    chk("en low holds acc", s.acc, 1)
    pe.step(a_in=3, b_in=3, last_in=1)
    s = pe.step()
    chk("resumes after en", s.drain_out, 10)
 
    # run_tile convenience path against the plain dot product
    pe2 = PEModel()
    pe2.reset_cycles(2)
    a = [7, -3, 120, -128, 0, 55]
    b = [-2, 9, -1, -128, 44, 6]
    chk("run_tile vs dot product", pe2.run_tile(a, b), expected_dot(a, b))
 
    # deliberate overflow at a narrowed accumulator, per contract 6.1
    narrow = PEModel(data_width=8, acc_width=18)
    narrow.reset_cycles(2)
    a = [127] * 16
    b = [127] * 16
    chk("narrow acc wraps not saturates",
        narrow.run_tile(a, b), expected_dot(a, b, acc_width=18))
 
    print("\nALL PASS" if fails == 0 else f"\n{fails} FAILURES")
    
    raise SystemExit(1 if fails else 0)