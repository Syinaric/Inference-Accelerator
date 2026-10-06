# Golden model for rtl/systolic_array.sv

from typing import List, NamedTuple

from pe_model import PEModel, wrap_signed, signed_range


class DelayLine:
    """Mirrors rtl/delay.sv for DEPTH >= 1.

    out() is the *current registered* value, readable before step() advances
    the line. That split matters: everything in the array samples on the same
    edge, so all wires must be read before any register commits.
    """

    def __init__(self, depth: int, ):
        if depth < 1:
            # systolic_array only instantiates DEPTH = r+1 and c+1, both >= 1.
            raise ValueError(f"DelayLine needs depth >= 1, got {depth}")
        self.depth = depth
        self.regs = [0] * depth

    def out(self) -> int:
        return self.regs[-1]

    def step(self, in_data: int, en: int = 1, reset: int = 0) -> None:
        if reset:
            self.regs = [0] * self.depth
            return
        if not en:
            return
        self.regs = [in_data] + self.regs[:-1]


class ArrayState(NamedTuple):
    """What is observable at the array boundary after one rising edge."""
    c_out: List[int]          # length N, index c = bottom of column c
    acc: List[List[int]]      # N x N, whitebox view for debugging only


class SystolicArrayModel:
    """Cycle-accurate model of an N x N output-stationary array."""

    def __init__(self, n: int = 8, data_width: int = 8, acc_width: int = 32):
        self.n = n
        self.data_width = data_width
        self.acc_width = acc_width
        self.a_skew = [DelayLine(r + 1) for r in range(n)]
        self.first_skew = [DelayLine(r + 1) for r in range(n)]
        self.last_skew = [DelayLine(r + 1) for r in range(n)]
        self.b_skew = [DelayLine(c + 1) for c in range(n)]

        self.pes = [[PEModel(data_width, acc_width) for _ in range(n)]
                    for _ in range(n)]
    @property
    def last_to_drain_ready(self) -> int:
        return 2 * self.n

    def c_out(self) -> List[int]:
        return [self.pes[self.n - 1][c].drain_out for c in range(self.n)]

    def acc_matrix(self) -> List[List[int]]:
        return [[self.pes[r][c].acc for c in range(self.n)] for r in range(self.n)]

    def state(self) -> ArrayState:
        return ArrayState(c_out=self.c_out(), acc=self.acc_matrix())

    def step(self,
             a_in: List[int] = None,
             b_in: List[int] = None,
             first_in: int = 0,
             last_in: int = 0,
             drain_shift: int = 0,
             en: int = 1,
             reset: int = 0) -> ArrayState:
        """Advance one rising edge and return the ReadOnly view after it."""
        n = self.n
        a_in = [0] * n if a_in is None else list(a_in)
        b_in = [0] * n if b_in is None else list(b_in)
        if len(a_in) != n or len(b_in) != n:
            raise ValueError(f"a_in and b_in must both have length N={n}")

        if reset:
            for line in self.a_skew + self.b_skew + self.first_skew + self.last_skew:
                line.step(0, reset=1)
            for row in self.pes:
                for pe in row:
                    pe.step(reset=1)
            return self.state()

        if not en:
            # Global stall. 
            return self.state()

        # phase 1
        
        a_h = [[0] * n for _ in range(n)]
        first_h = [[0] * n for _ in range(n)]
        last_h = [[0] * n for _ in range(n)]
        b_v = [[0] * n for _ in range(n)]
        d_v = [[0] * n for _ in range(n)]

        for r in range(n):
            a_h[r][0] = self.a_skew[r].out()
            first_h[r][0] = self.first_skew[r].out()
            last_h[r][0] = self.last_skew[r].out()
            for c in range(1, n):
                west = self.pes[r][c - 1]
                a_h[r][c] = west.a_out
                first_h[r][c] = west.first_out
                last_h[r][c] = west.last_out

        for c in range(n):
            b_v[0][c] = self.b_skew[c].out()
            # d_v[0][c] is tied to zero in the RTL: the top of every drain
            # chain shifts in zeros.
            d_v[0][c] = 0
            for r in range(1, n):
                north = self.pes[r - 1][c]
                b_v[r][c] = north.b_out
                d_v[r][c] = north.drain_out

        # phase 2
        for r in range(n):
            for c in range(n):
                self.pes[r][c].step(
                    a_in=a_h[r][c],
                    b_in=b_v[r][c],
                    first_in=first_h[r][c],
                    last_in=last_h[r][c],
                    drain_shift=drain_shift,
                    drain_in=d_v[r][c],
                    en=1,
                )

        # phase 3
        for r in range(n):
            self.a_skew[r].step(a_in[r])
            self.first_skew[r].step(int(bool(first_in)))
            self.last_skew[r].step(int(bool(last_in)))
        for c in range(n):
            self.b_skew[c].step(b_in[c])

        return self.state()

    def reset_cycles(self, cycles: int = 3) -> ArrayState:
        for _ in range(cycles):
            self.step(reset=1)
        return self.state()

    def run_tile(self, a_mat, b_mat) -> List[List[int]]:
        """Drive one N x K by K x N tile and drain it. Returns C as N x N.

        Sequencing here is the reference the controller must reproduce:
          K cycles of operands, then LAST_TO_DRAIN_READY idle cycles, then
          N-1 drain_shift cycles. c_out yields row N-1 first, then N-2 ... 0.
        """
        n = self.n
        k = len(a_mat[0])
        for j in range(k):
            self.step(
                a_in=[a_mat[r][j] for r in range(n)],
                b_in=[b_mat[j][c] for c in range(n)],
                first_in=int(j == 0),
                last_in=int(j == k - 1),
            )

        for _ in range(self.last_to_drain_ready):
            self.step()

        c = [[0] * n for _ in range(n)]
        c[n - 1] = self.c_out()
        for i in range(n - 1):
            self.step(drain_shift=1)
            c[n - 2 - i] = self.c_out()
        return c


def expected_matmul(a_mat, b_mat, acc_width: int = 32) -> List[List[int]]:
    """Independent reference. Deliberately knows nothing about the array."""
    n = len(a_mat)
    k = len(a_mat[0])
    cols = len(b_mat[0])
    return [
        [wrap_signed(sum(a_mat[r][j] * b_mat[j][c] for j in range(k)), acc_width)
         for c in range(cols)]
        for r in range(n)
    ]


if __name__ == "__main__":
    import random

    fails = 0

    def chk(name, got, exp):
        global fails
        if got != exp:
            print(f"FAIL {name}\n  got={got}\n  exp={exp}")
            fails += 1
        else:
            print(f"pass {name}")

    rng = random.Random(1234)

    for n, k in [(2, 1), (2, 4), (3, 3), (4, 4), (8, 8), (4, 17)]:
        lo, hi = signed_range(8)
        a = [[rng.randint(lo, hi) for _ in range(k)] for _ in range(n)]
        b = [[rng.randint(lo, hi) for _ in range(n)] for _ in range(k)]
        m = SystolicArrayModel(n=n)
        m.reset_cycles(3)
        chk(f"N={n} K={k} random tile", m.run_tile(a, b), expected_matmul(a, b))

    # signed extreme: every operand at -128, so every product is +16384
    n, k = 4, 8
    a = [[-128] * k for _ in range(n)]
    b = [[-128] * n for _ in range(k)]
    m = SystolicArrayModel(n=n)
    m.reset_cycles(3)
    chk("signed extreme -128", m.run_tile(a, b), expected_matmul(a, b))

    # no leakage: a second tile through the same model must be independent
    m2 = SystolicArrayModel(n=3)
    m2.reset_cycles(3)
    a1 = [[rng.randint(-128, 127) for _ in range(5)] for _ in range(3)]
    b1 = [[rng.randint(-128, 127) for _ in range(3)] for _ in range(5)]
    m2.run_tile(a1, b1)
    a2 = [[rng.randint(-128, 127) for _ in range(5)] for _ in range(3)]
    b2 = [[rng.randint(-128, 127) for _ in range(3)] for _ in range(5)]
    chk("back-to-back tile independent", m2.run_tile(a2, b2), expected_matmul(a2, b2))

    print("\nALL PASS" if fails == 0 else f"\n{fails} FAILURES")