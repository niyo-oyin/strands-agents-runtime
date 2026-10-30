# 共通の Python コマンドへのショートカット。Windows では uv run python scripts/dev.py <task> を直接使える。
.PHONY: help check lint typecheck test client-check validate integ docker-build docker-run hooks synth phoenix-up phoenix-down

UV ?= uv run
IMAGE ?= strands-runtime:dev
CONFIG ?= runtime.toml

help check lint typecheck test client-check validate integ docker-build docker-run hooks synth phoenix-up phoenix-down:
	$(UV) python scripts/dev.py $@ --config "$(CONFIG)" --image "$(IMAGE)"
