"""Dragon 主线成分等扫描共用工具。"""

from __future__ import annotations

from typing import List

import pandas as pd

from monthly_theme_dragon import DataStore, ScanResult


def theme_symbols(store: DataStore, scan: ScanResult, daily: pd.DataFrame) -> List[str]:
    """当日 T-001 激活主线成分（人工题材或行业）。"""
    if not scan.theme_active:
        return []
    if scan.theme_type == "manual":
        for t in store.load_manual_themes():
            if t.get("name") == scan.theme_name:
                return list(t.get("symbols") or [])
        return []
    ind = scan.theme_name
    return daily[daily["industry"] == ind]["symbol"].drop_duplicates().tolist()
