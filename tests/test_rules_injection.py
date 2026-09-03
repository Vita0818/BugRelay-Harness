from pathlib import Path

from core import rules


def _point_rules_at(monkeypatch, arena: Path):
    monkeypatch.setattr(rules, "_safe_arena", lambda: (arena, None))
    monkeypatch.setattr(rules, "log_event", lambda *args, **kwargs: None)


def test_injects_opencode_agents_and_detailed_guidelines(tmp_path, monkeypatch):
    arena = tmp_path / "arena"
    arena.mkdir()
    _point_rules_at(monkeypatch, arena)

    first = rules.inject_rules()
    assert first["ok"] and first["injected"]
    assert (arena / "TESTING_GUIDELINES.md").read_text(encoding="utf-8") == rules.RULES_MARKDOWN
    agents = (arena / "AGENTS.md").read_text(encoding="utf-8")
    assert "一次只处理一个问题" in agents
    assert "全新的同模型 Agent" in agents
    assert "不要故意出过难或含糊的题" in agents
    assert rules.rules_injected()

    second = rules.inject_rules()
    assert second["ok"] and not second["injected"]


def test_agents_managed_block_preserves_existing_project_instructions(tmp_path, monkeypatch):
    arena = tmp_path / "arena"
    arena.mkdir()
    original = "# Arena 自有规则\n\n保留这段项目说明。\n"
    (arena / "AGENTS.md").write_text(original, encoding="utf-8")
    _point_rules_at(monkeypatch, arena)

    assert rules.inject_rules()["ok"]
    merged = (arena / "AGENTS.md").read_text(encoding="utf-8")
    assert merged.startswith(original.rstrip())
    assert "保留这段项目说明。" in merged
    assert merged.count(rules.AGENTS_BLOCK_START) == 1
    assert merged.count(rules.AGENTS_BLOCK_END) == 1


def test_incomplete_managed_markers_fail_without_overwriting_agents(tmp_path, monkeypatch):
    arena = tmp_path / "arena"
    arena.mkdir()
    broken = rules.AGENTS_BLOCK_START + "\n不完整\n"
    agents_path = arena / "AGENTS.md"
    agents_path.write_text(broken, encoding="utf-8")
    _point_rules_at(monkeypatch, arena)

    result = rules.inject_rules()
    assert not result["ok"]
    assert agents_path.read_text(encoding="utf-8") == broken
