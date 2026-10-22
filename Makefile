# ローカル CI。GitHub Actions 等を載せるときも、この Makefile を呼ぶだけにする。
.PHONY: help check lint typecheck test validate integ docker-build docker-run hooks synth

UV ?= uv run
IMAGE ?= strands-runtime:dev
CONFIG ?= runtime.toml

help:
	@echo "make check        lint + typecheck + test + validate（コミット前の一式）"
	@echo "make lint         ruff format --check / ruff check"
	@echo "make typecheck    mypy --strict"
	@echo "make test         pytest（偽モデル、外部接続なし）"
	@echo "make validate     strands-runtime validate --mcp（runtime.toml と examples）"
	@echo "make integ        実モデルの結合テスト（.env の OPENROUTER_API_KEY が必要）"
	@echo "make docker-build linux/arm64 イメージをビルド（IMAGE=$(IMAGE)）"
	@echo "make docker-run   .env を渡してローカル実行（OTel 無効、8080）"
	@echo "make hooks        pre-commit の git フックを導入"
	@echo "make synth        CDK スタックを合成して cdk.out に出力（AWS アカウント不要）"

check: lint typecheck test validate

lint:
	$(UV) ruff format --check src tests tests_integ
	$(UV) ruff check src tests tests_integ

typecheck:
	$(UV) mypy

test:
	$(UV) pytest -q

validate:
	OPENROUTER_API_KEY=$${OPENROUTER_API_KEY:-validate-only} $(UV) strands-runtime -c $(CONFIG) validate
	OPENROUTER_API_KEY=$${OPENROUTER_API_KEY:-validate-only} $(UV) strands-runtime -c examples/feature-check/runtime.toml validate --mcp

integ:
	$(UV) --env-file .env pytest tests_integ -q

docker-build:
	docker buildx build --platform linux/arm64 -t $(IMAGE) --load .

docker-run:
	docker run --rm -p 8080:8080 --env-file .env -e OTEL_SDK_DISABLED=true $(IMAGE)

hooks:
	$(UV) pre-commit install --hook-type pre-commit --hook-type pre-push

synth:
	$(UV) python -m infra.app
