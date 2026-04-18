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
        defaults: Iterable[tuple[str, str, str, str]] = [
            ("Atlas CEO", "CEO", "Executive", "Own the lab and manage agents."),
            ("Iris ChiefOfStaff", "Chief of Staff", "Executive", "Track execution and dependencies."),
            ("Vega MarketIntel", "Market Intelligence", "Research", "Monitor market structure."),
            ("Nova StrategyRND", "Strategy R&D", "Research", "Generate hypotheses."),
            ("Sigma Quant", "Quant Research", "Research", "Design signals and validations."),
            ("Helix Backtest", "Backtest", "Simulation", "Run backtests and stress tests."),
            ("Sentinel Risk", "Risk & Governance", "Governance", "Enforce risk gates."),
            ("Archive Memory", "Memory Architect", "Knowledge", "Compress and store lessons."),
            ("Pulse Analytics", "Performance Analytics", "Analytics", "Score experiments."),
            ("Forge Execution", "Execution Engineer", "Execution", "Own paper/live execution interfaces."),
        ]
        created: list[AgentProfile] = []
        for name, role, department, mandate in defaults:
            if self.find_by_name(name) is None:
                created.append(self.add(AgentProfile(name=name, role=role, department=department, mandate=mandate)))
        return created
