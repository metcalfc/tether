.DEFAULT_GOAL := help
# The demo's gateway config: the compiled policy plus a second sandbox for the replay step.
POLICY ?= overnight/run/compiled/tether-replay.json
.PHONY: help test lint compile demo-setup demo-teardown record report audit

help:
	@echo 'make test           run the unit tests (no sandbox, no keys)'
	@echo 'make lint           ruff and shellcheck'
	@echo ''
	@echo 'the microsandbox + nono cage (see overnight/REPORT.md for prerequisites):'
	@echo 'make compile        spike/msb/poc-policy.json -> msb flags, nono profiles, tether config'
	@echo 'make demo-setup     fresh tether state, compile, gateway up'
	@echo '                    then spike/msb/demo.sh 1 .. 10, one step each'
	@echo 'make demo-teardown  cage down, gateway down'
	@echo 'make record         record the whole demo with VHS'
	@echo 'make report         spend and denials per (session, agent, tool)'
	@echo 'make audit          verify the audit hash chain'

test:
	python3 -m unittest discover -s tests -v

lint:
	ruff check --select E,F,W,I,B,UP --line-length 120 tether tests/ spike/msb/*.py
	shellcheck spike/msb/*.sh spike/msb/guest/*.sh spike/msb/kernel/*.sh overnight/*.sh

compile:
	spike/msb/cage.sh compile

demo-setup:
	spike/msb/demo.sh setup

demo-teardown:
	spike/msb/demo.sh teardown

record:
	vhs spike/record/poc-demo.tape

report:
	./tether -c $(POLICY) report --by tool

audit:
	./tether -c $(POLICY) audit verify
