"""Application settings — user-configurable knobs (auto-approve vb.).

artifacts/settings.json içinde tutulur. Dashboard üzerinden değiştirilebilir.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


_SETTINGS_FILE = Path("artifacts/settings.json")


@dataclass
class AppSettings:
    # Auto-approve (7/24 fabrika modu)
    auto_approve: bool = False
    auto_approve_delay_minutes: int = 10         # bekleme süresi (manuel aksiyona şans)
    auto_approve_max_risk: str = "medium"        # critical reddedilsin, medium ve altı onaylansın
    auto_approve_allowed_types: list[str] = None  # None = hepsi, aksi halde whitelist

    # Auto-reject (riskli olanları otomatik reddet)
    auto_reject_critical: bool = True

    # Hermes GitResearcher otomatik taraması
    hermes_auto_scan: bool = False
    hermes_scan_interval_hours: int = 24

    def __post_init__(self) -> None:
        if self.auto_approve_allowed_types is None:
            # Default güvenli tipler: integration, curriculum, new_feature (param_override hariç)
            self.auto_approve_allowed_types = ["integration", "curriculum", "new_feature", "new_strategy"]


def load_settings() -> AppSettings:
    if not _SETTINGS_FILE.exists():
        return AppSettings()
    try:
        raw = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
        return AppSettings(**{
            k: v for k, v in raw.items()
            if k in AppSettings.__dataclass_fields__
        })
    except Exception:
        return AppSettings()


def save_settings(settings: AppSettings) -> None:
    _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_FILE.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def update_settings(**kwargs) -> AppSettings:
    s = load_settings()
    for k, v in kwargs.items():
        if k in AppSettings.__dataclass_fields__:
            setattr(s, k, v)
    save_settings(s)
    return s
