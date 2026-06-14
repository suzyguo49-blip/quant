"""Tushare token 配置（勿提交 token 到 git）。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "docs/trading-system/tushare.yaml"


def load_tushare_token(config_path: Optional[Path] = None) -> str:
    token = (os.environ.get("TUSHARE_TOKEN") or "").strip()
    if token:
        return token

    path = config_path or DEFAULT_CONFIG
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        token = (data.get("token") or "").strip()
        if token:
            return token

    raise FileNotFoundError(
        f"未配置 Tushare token。请复制 docs/trading-system/tushare.yaml.example "
        f"为 tushare.yaml 并填写 token，或设置环境变量 TUSHARE_TOKEN"
    )
