from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
import json
from typing import Iterable

from oto_bot.core.models import AgentProfile


class AgentRegistry:
    def __init__(self, path: str | Path = "memories/agents.json") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._agents: dict[str, AgentProfile] = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self._agents = {}
            return
        raw = json.loads(self.path.read_text())
        self._agents = {}
        for item in raw:
            item["created_at"] = datetime.fromisoformat(item["created_at"])
            self._agents[item["agent_id"]] = AgentProfile(**item)

    def save(self) -> None:
        payload = []
        for agent in self._agents.values():
            row = asdict(agent)
            row["created_at"] = agent.created_at.isoformat()
            payload.append(row)
        self.path.write_text(json.dumps(payload, indent=2))

    def add(self, profile: AgentProfile) -> AgentProfile:
        self._agents[profile.agent_id] = profile
        self.save()
        return profile

    def all(self) -> list[AgentProfile]:
        return list(self._agents.values())

    def active(self) -> list[AgentProfile]:
        return [agent for agent in self._agents.values() if agent.active]

    def find_by_name(self, name: str) -> AgentProfile | None:
        return next((a for a in self._agents.values() if a.name == name), None)

    def retire(self, agent_id: str) -> None:
        if agent_id in self._agents:
            self._agents[agent_id].active = False
            self.save()

    def seed_defaults(self) -> list[AgentProfile]:
        # Faz 4: "wired" ajanlar artik canli kod yoluna baglandi.
        # Bu metadata HR review'larinda ve dashboard'larda "yasayan" agent
        # olarak gozukmesini saglar — onceden dead code olduklarini gizleyen
        # asagilayici "(no live wiring)" notu kaldirildi.
        wired = {"Apex PortfolioRisk", "Ledger Allocator"}

        defaults: Iterable[tuple[str, str, str, str]] = [
            # Executive
            ("Atlas CEO", "Head of Trading", "Executive", "Own the book; direct all departments; final promotion authority."),
            ("Iris ChiefOfStaff", "Chief of Staff", "Executive", "Track execution, dependencies, and committee follow-ups."),

            # Research
            ("Vega MarketIntel", "Market Intelligence", "Research", "Scan markets for regime-appropriate opportunities."),
            ("Nova StrategyRND", "Strategy R&D", "Research", "Generate hypotheses and strategy variants."),
            ("Sigma Quant", "Quant Research", "Research", "Validate statistical significance and detect overfitting."),
            ("Mercury Macro", "Macro Strategist", "Research", "Cross-asset overlay: risk-on/off, dominance, macro bias."),
            ("Regime Oracle", "Regime Classifier", "Research", "Classify market regimes per instrument."),

            # Simulation
            ("Helix Backtest", "Backtest", "Simulation", "Run backtests with realistic execution assumptions."),
            ("Shockwave StressLab", "Stress Lab", "Simulation", "Run named stress scenarios against promotion candidates."),

            # Governance / Risk
            ("Sentinel Risk", "Risk Gatekeeper", "Governance", "Enforce per-strategy risk policy gates."),
            ("Apex PortfolioRisk", "Portfolio Risk", "Governance", "Independent book-level risk; VaR/ES/correlation; veto authority."),
            ("Cassandra PreMortem", "Pre-Mortem Analyst", "Governance", "Systematic failure-mode scan before promotion."),

            # Execution
            ("Forge Execution", "Execution Engineer", "Execution", "Paper trading execution, kill-switch, order management."),
            ("Tariq TCA", "Execution QA", "Execution", "Transaction cost analysis; slippage/impact/latency measurement."),
            ("Ledger Allocator", "Capital Allocator", "Execution", "Pod capital allocation, rebalance, auto stop-out."),

            # Knowledge / Analytics
            ("Archive Memory", "Memory Architect", "Knowledge", "Compress, index, and archive experiment history."),
            ("Pulse Analytics", "Performance Analytics", "Analytics", "Score strategies on risk-adjusted metrics."),
            ("Ledger Attribution", "PnL Attribution", "Analytics", "Decompose PnL by signal, symbol, regime, hour."),
        ]
        created: list[AgentProfile] = []
        for name, role, department, mandate in defaults:
            existing = self.find_by_name(name)
            if existing is None:
                meta = {"wired_phase": 4} if name in wired else {}
                created.append(self.add(AgentProfile(
                    name=name, role=role, department=department,
                    mandate=mandate, metadata=meta,
                )))
            elif name in wired and existing.metadata.get("wired_phase") != 4:
                # Mevcut kayit varsa metadata'yi guncelle (geri uyumluluk)
                existing.metadata["wired_phase"] = 4
                existing.active = True
                self.save()
        return created
