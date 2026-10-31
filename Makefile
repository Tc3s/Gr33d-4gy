# Makefile for agy-supervisor
.PHONY: all help test e2e test-all check health verify install uninstall clean

PYTHON ?= python3

all: help

help:
	@echo "======================================================================"
	@echo "  agy-supervisor Developer & Admin Automation Tools"
	@echo "======================================================================"
	@echo "Available targets:"
	@echo "  make test        - Run the master regression unit test suite (178 tests)"
	@echo "  make e2e         - Run the 4-tier opaque-box E2E test suite (115 tests)"
	@echo "  make test-all    - Run both master unit tests and all E2E tests"
	@echo "  make health      - Run environment and credential health check audit"
	@echo "  make verify      - Run live deterministic quota failover PoC simulation"
	@echo "  make install     - Install binary to ~/.local/bin/ and setup bash alias"
	@echo "  make uninstall   - Remove binary, aliases, and clean configurations"
	@echo "  make clean       - Remove cached Python bytecode and temp test files"
	@echo "======================================================================"

test:
	$(PYTHON) tests/test_supervisor.py

e2e:
	pytest tests/e2e/ -v

test-all: test e2e

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
