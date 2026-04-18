from oto_bot.backtest.engine import BacktestEngine
from oto_bot.strategies.base import StrategyContext
from oto_bot.strategies.day_trader import DayTraderStrategy
from oto_bot.utils.data import make_synthetic_ohlc


def test_backtest_runs():
    engine = BacktestEngine()
    data = make_synthetic_ohlc(300)
    ctx = StrategyContext(market="crypto", strategy_family="day", symbol="BTCUSDT", timeframe="1h")
    result = engine.run(DayTraderStrategy(), data, ctx)
    assert result.experiment_id
