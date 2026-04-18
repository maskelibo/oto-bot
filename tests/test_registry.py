from oto_bot.agents.registry import AgentRegistry


def test_seed_defaults(tmp_path):
    registry = AgentRegistry(tmp_path / "agents.json")
    created = registry.seed_defaults()
    assert len(registry.all()) >= len(created)
    assert registry.find_by_name("Atlas CEO") is not None
