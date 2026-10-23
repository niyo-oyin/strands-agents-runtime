"""CDK アプリの入口。``uv run python -m infra.app`` で直接、または cdk.json 経由で ``npx aws-cdk synth|deploy`` から使う。"""

from __future__ import annotations

import os
from pathlib import Path

from aws_cdk import App

from infra.spec import load_deployments
from infra.stack import StrandsRuntimeStack

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_app(deployments_path: Path = REPO_ROOT / "infra" / "deployments.toml", *, outdir: str | None = None) -> App:
    """deployments.toml を読んでスタックを 1 つ持つ App を作る。``outdir`` が None なら CDK が一時ディレクトリを使う。"""
    deployments = load_deployments(deployments_path)
    app = App(outdir=outdir)
    StrandsRuntimeStack(app, deployments.stack.name, repo_root=REPO_ROOT, deployments=deployments)
    return app


if __name__ == "__main__":
    # CDK CLI から呼ばれるときは CDK_OUTDIR が設定される。直接実行したときは ./cdk.out に出力する。
    build_app(outdir=os.environ.get("CDK_OUTDIR", "cdk.out")).synth()
