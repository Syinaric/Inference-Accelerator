#golden reference model for the delay/skew module 


def expected_out (history, cycle, depth): 
    """ the expected out_data from the read only section after the "rising edge" cycle"""

    latency = depth - 1 if depth > 0 else 0 
    idx = cycle - latency 
    if idx < 0: 
        return 0     #pipleine not full yet 
    return history[idx] 





