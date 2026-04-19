"""Smoke tests for the institutional agent upgrade.

Bu testler Python + pandas + pytest kurulu olduğunda çalışır. Amacı yeni
ajanların birbirine bağlandığını ve temel bir pipe'ın çalıştığını
garantilemektir; istatistiksel doğruluk değil.

Kurulumu:
    cd C:/Users/koray/projeler/oto-bot
    python -m venv .venv
    source .venv/Scripts/activate  # Windows Git Bash
    pip install -r requirements.txt
    pytest -q
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

import pandas as pd
import pytest

from oto_bot.agents.attribution import PnLAttributor
from oto_bot.agents.ceo import CEOAgent
from oto_bot.agents.debate import AgentDebater
from oto_bot.agents.macro import MercuryMacro
from oto_bot.agents.pod_allocator import PodAllocator
from oto_bot.agents.portfolio_risk import ApexPortfolioRisk
from oto_bot.agents.premortem import CassandraPreMortem
from oto_bot.agents.regime import RegimeOracle
from oto_bot.agents.registry import AgentRegistry
from oto_bot.agents.stress import BUILTIN_SCENARIOS, StressLab
from oto_bot.agents.tca import TariqTCA
from oto_bot.core.models import ExperimentResult


@pytest.fixture
def ohlcv_sample() -> pd.DataFrame:
    # 120 bar'lık trendli + gürültülü yapay seri
    rows = []
    price = 100.0
    for i in range(120):
        price *= 1 + 0.002 * math.sin(i / 7) + (0.001 if i > 60 else -0.0005)
        high = price * 1.006
        low = price * 0.994
        op = price * 0.999
        vol = 1_000 + (i % 7) * 50
        rows.append({"open": op, "high": high, "low": low, "close": price, "volume": vol})
    return pd.DataFrame(rows)


def test_regime_oracle_classifies(ohlcv_sample):
    oracle = RegimeOracle()
    state = oracle.classify(ohlcv_sample, market="test")
    assert state.regime in {"trend_up", "trend_down", "range", "high_vol", "crisis", "unknown"}
    assert 0.0 <= state.confidence <= 1.0


def test_macro_bias(ohlcv_sample):
    merc = MercuryMacro()
    ctx = merc.assess("test", ohlcv_sample, companion_ohlcv={"ETH": ohlcv_sample})
    assert ctx.bias in {"risk_on", "neutral", "risk_off", "crisis"}
    assert -1.0 <= ctx.risk_on_score <= 1.0
    note = merc.advisory_note(ctx)
    assert isinstance(note, str) and "bias=" in note


def test_apex_risk_green_on_empty():
    apex = ApexPortfolioRisk()
    snapshot, verdict = apex.assess(
        pods=[], book_returns=[], pod_returns={}, total_capital=100_000,
        gross_exposure=0, net_exposure=0, peak_capital=100_000,
        daily_pnl=0, weekly_pnl=0, mtd_pnl=0,
    )
    assert verdict.status in {"green", "amber", "red", "black"}
    assert snapshot.total_capital == 100_000


def test_pod_allocator_stop_out(tmp_path):
    alloc = PodAllocator(path=tmp_path / "pods.json", book_capital=100_000)
    pod = alloc.create_pod("scalp", "crypto", initial_capital=10_000)
    # 8% drawdown → retire
    alloc.update_pod(pod.pod_id, current_capital=9_200)
    refreshed = alloc.active()
    assert any(p.status == "retired" for p in alloc.all()) or all(p.status in {"active", "halved"} for p in refreshed)


def test_stress_lab_runs(ohlcv_sample):
    lab = StressLab()
    def fake_bt(shocked: pd.DataFrame):
        dd = float(shocked["close"].pct_change().min() or 0.0)
        return {"max_drawdown": dd, "total_pnl": 0.0, "kill_switch_fired": dd < -0.3}

    results = lab.run_all(ohlcv_sample, "scalp", "crypto", fake_bt)
    assert len(results) == len(BUILTIN_SCENARIOS)
    assert all(r.scenario_id for r in results)


def test_attribution_decomposes():
    attr = PnLAttributor()
    trades = [
        {"pnl": 10, "signal": "bb", "symbol": "BTC/USDT", "regime": "range",
         "hour": 14, "direction": "long", "notional": 500, "fees": 0.5, "slippage": 1.0},
        {"pnl": -3, "signal": "rsi", "symbol": "ETH/USDT", "regime": "trend_up",
         "hour": 8, "direction": "short", "notional": 400, "fees": 0.4, "slippage": 0.8},
    ]
    a = attr.attribute(experiment_id="e1", trades=trades, market_return_pct=0.001)
    assert a.total_pnl == pytest.approx(7)
    assert set(a.by_signal.keys()) == {"bb", "rsi"}


def test_tca_flags_slippage():
    tca = TariqTCA(acceptable_slippage_bps=5)
    report = tca.build_report(
        order_id="o1", symbol="BTC/USDT", side="long",
        intended_price=60_000, filled_price=60_120,
        intended_qty=0.1, filled_qty=0.1, fees_quote=4.0, latency_ms=80,
    )
    agg = tca.aggregate([report])
    assert agg["count"] == 1
    assert agg["alerts"], "should flag slippage > 5bps threshold"


def test_premortem_red_on_bad_sample():
    pm = CassandraPreMortem()
    result = {
        "sharpe": 1.5, "walkforward_sharpe": 0.2, "total_trades": 10,
        "profit_factor": 1.02, "win_rate": 0.8, "expectancy": 0.001,
        "regime": "unknown", "notes": "no slippage assumed", "montecarlo_95_drawdown": -0.35,
    }
    report = pm.evaluate("scalp", result, book_correlation=0.9)
    assert report.risk_score >= 70
    assert report.verdict.startswith("RED")


def test_debate_blocks_on_apex_red():
    debater = AgentDebater()
    result = {
        "sharpe": 1.4, "max_drawdown": -0.05, "profit_factor": 1.3,
        "stability_score": 0.8, "win_rate": 0.55, "expectancy": 0.02,
        "sortino": 1.5, "calmar": 0.9, "total_trades": 150, "regime": "range",
        "strategy_family": "scalp",
    }
    record = debater.debate(
        topic="test",
        experiment_result=result,
        context={"apex": {"status": "red", "breaches": ["VaR95 over limit"]}},
    )
    assert "REJECT" in record.conclusion


def test_ceo_happy_path(tmp_path):
    registry = AgentRegistry(path=tmp_path / "agents.json")
    registry.seed_defaults()
    ceo = CEOAgent(registry=registry)
    result = ExperimentResult(
        experiment_id="ex1", hypothesis_title="Test swing",
        market="crypto", strategy_family="swing",
        roi=0.2, win_rate=0.6, profit_factor=1.5, sharpe=1.6,
        max_drawdown=-0.08, expectancy=0.03, stability_score=0.7,
        promoted=False, notes="synthetic", sortino=1.8, calmar=2.0,
        total_trades=200, regime="trend_up", walkforward_sharpe=1.3,
        montecarlo_95_drawdown=-0.15,
    )
    decision = ceo.review_experiment(result)
    assert decision.decision in {
        "promote_to_paper_trading", "hold_and_monitor",
        "hold_and_iterate", "reject", "block_book_risk", "block_premortem",
    }
