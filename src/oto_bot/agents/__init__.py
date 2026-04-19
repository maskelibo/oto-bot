"""Agent package — institutional multi-voice architecture.

Executive:
    CEOAgent (Atlas — Head of Trading)
    AgentRegistry (Iris + roster)

Governance / risk:
    ApexPortfolioRisk (independent book-level risk)
    CassandraPreMortem (failure-mode forecaster)

Research overlays:
    RegimeOracle (market regime classifier)
    MercuryMacro (cross-asset overlay)

Execution:
    PodAllocator (capital allocation, auto stop-out)
    TariqTCA (execution quality)

Analytics:
    PnLAttributor (PnL decomposition)

Panel:
    AgentDebater (8-voice debate + CEO conclusion)

Stress:
    StressLab + BUILTIN_SCENARIOS
"""

from oto_bot.agents.attribution import PnLAttributor
from oto_bot.agents.ceo import CEOAgent
from oto_bot.agents.debate import AgentDebater, DebateRecord
from oto_bot.agents.macro import MercuryMacro
from oto_bot.agents.pod_allocator import PodAllocator
from oto_bot.agents.portfolio_risk import ApexPortfolioRisk, RiskVerdict
from oto_bot.agents.premortem import CassandraPreMortem, PreMortemReport
from oto_bot.agents.regime import RegimeOracle
from oto_bot.agents.registry import AgentRegistry
from oto_bot.agents.stress import BUILTIN_SCENARIOS, StressLab
from oto_bot.agents.tca import TariqTCA

__all__ = [
    "AgentDebater",
    "AgentRegistry",
    "ApexPortfolioRisk",
    "BUILTIN_SCENARIOS",
    "CEOAgent",
    "CassandraPreMortem",
    "DebateRecord",
    "MercuryMacro",
    "PnLAttributor",
    "PodAllocator",
    "PreMortemReport",
    "RegimeOracle",
    "RiskVerdict",
    "StressLab",
    "TariqTCA",
]
