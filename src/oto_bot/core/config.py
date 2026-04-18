"""Centralised, config-driven settings for oto-bot.

Loads environment variables via python-dotenv, then merges YAML config files
(risk, organization, strategies) into a single Settings singleton that the
rest of the codebase imports.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Project root is three levels up from this file:
#   src/oto_bot/core/config.py  ->  project root
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Pydantic models for typed, validated config sections
# ---------------------------------------------------------------------------

class RiskConfig(BaseModel):
    max_drawdown_limit: float = -0.15
    min_sharpe: float = 1.0
    min_profit_factor: float = 1.1
    single_strategy_daily_loss_cap: float = 0.02
    portfolio_daily_loss_cap: float = 0.04


class DayTraderParams(BaseModel):
    fast_ma: int = 5
    slow_ma: int = 20
    atr_period: int = 14
    atr_multiplier_sl: float = 1.5
    atr_multiplier_tp: float = 2.5
    volume_filter: float = 1.2
    rsi_period: int = 14
    rsi_overbought: int = 70
    rsi_oversold: int = 30


class SwingTraderParams(BaseModel):
    ma_short: int = 50
    ma_long: int = 200
    atr_period: int = 14
    atr_multiplier_sl: float = 2.0
    atr_multiplier_tp: float = 3.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9


class ScalperParams(BaseModel):
    lookback: int = 10
    z_entry: float = 1.0
    z_exit: float = 0.0
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    atr_multiplier_sl: float = 0.8


class StrategiesConfig(BaseModel):
    day_trader: DayTraderParams = Field(default_factory=DayTraderParams)
    swing_trader: SwingTraderParams = Field(default_factory=SwingTraderParams)
    scalper: ScalperParams = Field(default_factory=ScalperParams)


class OrganizationConfig(BaseModel):
    executive: dict[str, str] = Field(default_factory=dict)
    departments: list[str] = Field(default_factory=list)
    strategy_families: list[str] = Field(default_factory=list)
    markets: list[str] = Field(default_factory=list)


class Settings(BaseModel):
    """Top-level settings singleton that aggregates all config sources."""

    # --- .env values -------------------------------------------------------
    app_env: str = "dev"
    log_level: str = "INFO"
    experiment_db_path: str = "artifacts/experiments.sqlite3"
    default_markets: list[str] = Field(
        default_factory=lambda: ["crypto", "forex", "us_equities", "bist"]
    )
    enable_live_trading: bool = False
    paper_trading_start_balance: float = 100_000.0
    ceo_name: str = "Atlas CEO"

    # --- YAML sections -----------------------------------------------------
    risk: RiskConfig = Field(default_factory=RiskConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    organization: OrganizationConfig = Field(default_factory=OrganizationConfig)

    # --- Resolved project root (not serialised) ----------------------------
    project_root: Path = _PROJECT_ROOT

    model_config = {"arbitrary_types_allowed": True}


# ---------------------------------------------------------------------------
# YAML loader helper
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict, or empty dict."""
    if not path.is_file():
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_settings_instance: Settings | None = None


def get_settings(*, reload: bool = False) -> Settings:
    """Return the global Settings singleton, creating it on first call.

    Set *reload=True* to force re-reading every config source (useful in
    tests or after hot-reloading YAML files).
    """
    global _settings_instance
    if _settings_instance is not None and not reload:
        return _settings_instance

    # 1. Load .env ---------------------------------------------------------
    env_path = _PROJECT_ROOT / ".env"
    load_dotenv(env_path, override=True)

    # 2. Read env vars with fallbacks --------------------------------------
    default_markets_raw = os.getenv("DEFAULT_MARKETS", "crypto,forex,us_equities,bist")
    default_markets = [m.strip() for m in default_markets_raw.split(",") if m.strip()]

    enable_live = os.getenv("ENABLE_LIVE_TRADING", "false").lower() in ("true", "1", "yes")

    env_values: dict[str, Any] = {
        "app_env": os.getenv("APP_ENV", "dev"),
        "log_level": os.getenv("LOG_LEVEL", "INFO"),
        "experiment_db_path": os.getenv("EXPERIMENT_DB_PATH", "artifacts/experiments.sqlite3"),
        "default_markets": default_markets,
        "enable_live_trading": enable_live,
        "paper_trading_start_balance": float(
            os.getenv("PAPER_TRADING_START_BALANCE", "100000")
        ),
        "ceo_name": os.getenv("CEO_NAME", "Atlas CEO"),
    }

    # 3. Load YAML configs -------------------------------------------------
    configs_dir = _PROJECT_ROOT / "configs"

    risk_data = _load_yaml(configs_dir / "risk.yaml")
    org_data = _load_yaml(configs_dir / "organization.yaml")
    strat_data = _load_yaml(configs_dir / "strategies.yaml")

    env_values["risk"] = RiskConfig(**risk_data) if risk_data else RiskConfig()
    env_values["organization"] = OrganizationConfig(**org_data) if org_data else OrganizationConfig()
    env_values["strategies"] = StrategiesConfig(**strat_data) if strat_data else StrategiesConfig()
    env_values["project_root"] = _PROJECT_ROOT

    _settings_instance = Settings(**env_values)
    return _settings_instance
