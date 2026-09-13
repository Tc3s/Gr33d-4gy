# Makefile for agy-supervisor
.PHONY: all help test check health verify install uninstall clean

PYTHON ?= python3

all: help

help:
	@echo "======================================================================"
	@echo "  agy-supervisor Developer & Admin Automation Tools"
	@echo "======================================================================"
	@echo "Available targets:"
	@echo "  make test        - Run the regression and edge-case test suite (17 tests)"
	@echo "  make health      - Run environment and credential health check audit"
	@echo "  make verify      - Run live deterministic quota failover PoC simulation"
	@echo "  make install     - Install binary to ~/.local/bin/ and setup bash alias"
	@echo "  make uninstall   - Remove binary, aliases, and clean configurations"
	@echo "  make clean       - Remove cached Python bytecode and temp test files"
	@echo "======================================================================"

test:
	$(PYTHON) tests/test_supervisor.py

health: check
check:
	$(PYTHON) scripts/health_check.py

verify:
	$(PYTHON) scripts/verify_live_rotation.py

install:
	./install.sh

uninstall:
	./uninstall.sh

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type f -name "*.pyo" -delete
	rm -f bin/test.json
