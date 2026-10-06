SIM ?= icarus
TOPLEVEL_LANG ?= verilog

MODULE_NAME ?= delay

DEPTH      ?= 4
DATA_WIDTH ?= 8
ACC_WIDTH  ?= 32
K          ?= 8
N          ?= 8
SEED ?=
export SEED
export K
export N

# Find RTL by module name inside any block folder (systolic_array/rtl, memory/rtl, ...)
RTL_ALL := $(wildcard $(PWD)/*/rtl/*.sv)
VERILOG_SOURCES += $(filter %/$(MODULE_NAME).sv,$(RTL_ALL))

COCOTB_TOPLEVEL     = $(MODULE_NAME)
COCOTB_TEST_MODULES = test_$(MODULE_NAME)

# Every block's tb/ and model/ folder is importable, so imports stay flat
empty :=
space := $(empty) $(empty)
export PYTHONPATH := $(subst $(space),:,$(wildcard $(PWD)/*/tb $(PWD)/*/model)):$(PYTHONPATH)

COMPILE_ARGS += -g2012

ifeq ($(MODULE_NAME),delay)
COMPILE_ARGS += -Pdelay.DEPTH=$(DEPTH)
COMPILE_ARGS += -Pdelay.DATA_WIDTH=$(DATA_WIDTH)
SIM_BUILD = sim_build/delay_depth_$(DEPTH)
endif

ifeq ($(MODULE_NAME),pe)
COMPILE_ARGS += -Ppe.DATA_WIDTH=$(DATA_WIDTH)
COMPILE_ARGS += -Ppe.ACC_WIDTH=$(ACC_WIDTH)
SIM_BUILD = sim_build/pe_k_$(K)_acc_$(ACC_WIDTH)
endif

# The array pulls in its children. Icarus does not care about file order.
ifeq ($(MODULE_NAME),systolic_array)
VERILOG_SOURCES += $(PWD)/systolic_array/rtl/pe.sv $(PWD)/systolic_array/rtl/delay.sv
COMPILE_ARGS += -Psystolic_array.N=$(N)
COMPILE_ARGS += -Psystolic_array.DATA_WIDTH=$(DATA_WIDTH)
COMPILE_ARGS += -Psystolic_array.ACC_WIDTH=$(ACC_WIDTH)
SIM_BUILD = sim_build/array_n_$(N)_k_$(K)
endif

include $(shell cocotb-config --makefiles)/Makefile.sim

.PHONY: sweep sweep-pe sweep-array sweep-all

sweep:
	@for d in 0 1 2 4 8; do \
	  echo "=== DEPTH=$$d ==="; \
	  $(MAKE) -s MODULE_NAME=delay DEPTH=$$d || exit 1; \
	done
	@echo "=== delay sweep passed ==="

sweep-pe:
	@for k in 1 2 8 64 255; do \
	  echo "=== K=$$k ==="; \
	  $(MAKE) -s MODULE_NAME=pe K=$$k || exit 1; \
	done
	@echo "=== K=16 ACC_WIDTH=18 (deliberate overflow) ==="
	@$(MAKE) -s MODULE_NAME=pe K=16 ACC_WIDTH=18 || exit 1
	@echo "=== pe sweep passed ==="

sweep-array:
	@for n in 2 3 4 8; do \
	  for k in 1 4 16; do \
	    echo "=== N=$$n K=$$k ==="; \
	    $(MAKE) -s MODULE_NAME=systolic_array N=$$n K=$$k || exit 1; \
	  done; \
	done
	@echo "=== array sweep passed ==="

sweep-all: sweep sweep-pe sweep-array