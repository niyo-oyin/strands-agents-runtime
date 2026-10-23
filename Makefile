# ローカル CI。GitHub Actions 等を載せるときも、この Makefile を呼ぶだけにする。
.PHONY: help check lint typecheck test validate integ docker-build docker-run hooks synth phoenix-up phoenix-down

UV ?= uv run
IMAGE ?= strands-runtime:dev
CONFIG ?= runtime.toml

help:
	@echo "make check        lint + typecheck + test + validate（コミット前の一式）"
	@echo "make lint         ruff format --check / ruff check"
	@echo "make typecheck    mypy --strict"
	@echo "make test         pytest（偽モデル、外部接続なし）"
	@echo "make validate     strands-runtime validate --mcp（runtime.toml、examples/feature-check、examples/reference）"
	@echo "make integ        実モデルの結合テスト（.env の OPENROUTER_API_KEY が必要）"
	@echo "make docker-build linux/arm64 イメージをビルド（IMAGE=$(IMAGE)）"
	@echo "make docker-run   .env を渡してローカル実行（OTel 無効、8080）"
	@echo "make hooks        pre-commit の git フックを導入"
	@echo "make synth        CDK スタックを合成して cdk.out に出力（AWS アカウント不要）"
	@echo "make phoenix-up   Arize Phoenix（OTLP 受信 + UI http://localhost:6006）を Docker で起動"
	@echo "make phoenix-down Phoenix を停止"

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
	OPENROUTER_API_KEY=$${OPENROUTER_API_KEY:-validate-only} $(UV) strands-runtime -c examples/reference/runtime.toml validate --mcp

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

# LLM 可視化の確認用。起動後、次の環境変数を付けて run / serve するとトレースが届く:
#   OTEL_TRACES_EXPORTER=otlp OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:6006 OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
phoenix-up:
	docker rm -f strands-phoenix >/dev/null 2>&1 || true
	docker run -d --name strands-phoenix -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest >/dev/null
	@for i in $$(seq 1 90); do curl -sf http://localhost:6006/healthz >/dev/null && break; sleep 1; done
	@echo "Phoenix: http://localhost:6006"

phoenix-down:
	docker rm -f strands-phoenix >/dev/null 2>&1 || true
