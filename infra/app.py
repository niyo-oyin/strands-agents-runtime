"""CDK app entry point: ``uv run python -m infra.app`` (or ``npx aws-cdk synth|deploy`` via cdk.json)."""

from __future__ import annotations

import os
from pathlib import Path

from aws_cdk import App

from infra.spec import load_deployments
from infra.stack import StrandsRuntimeStack

REPO_ROOT = Path(__file__).resolve().parents[1]


def build_app(deployments_path: Path = REPO_ROOT / "infra" / "deployments.toml", *, outdir: str | None = None) -> App:
    deployments = load_deployments(deployments_path)
    app = App(outdir=outdir)
    StrandsRuntimeStack(app, deployments.stack.name, repo_root=REPO_ROOT, deployments=deployments)
    return app


if __name__ == "__main__":
    # The CDK CLI sets CDK_OUTDIR; run directly, write the cloud assembly to ./cdk.out.
    build_app(outdir=os.environ.get("CDK_OUTDIR", "cdk.out")).synth()
