from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest

from core import judge, repo_ops


def _part(*, passed=0, failed=0, errors=0, skipped=0, total=0):
    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "total": total,
    }


def _result(history, hidden, exit_code, reliable=True):
    return {
        "history": history,
        "hidden": hidden,
        "exit_code": exit_code,
        "passed": history["passed"] + hidden["passed"],
        "total": history["total"] + hidden["total"],
        "ok": exit_code == 0,
        "stats_reliable": reliable,
        "log_text": "",
        "hidden_dest": None,
    }


def _red_result():
    return _result(
        _part(passed=2, total=2),
        _part(failed=3, total=3),
        exit_code=1,
    )


def _green_result():
    return _result(
        _part(passed=2, total=2),
        _part(passed=3, total=3),
        exit_code=0,
    )


def test_strict_red_and_green_gates_reject_vacuous_results():
    assert judge._validate_all_red(_red_result(), 3) == (True, "")
    assert judge._validate_all_green(_green_result(), 3) == (True, "")

    all_green_before_change = _result(
        _part(passed=2, total=2), _part(passed=3, total=3), exit_code=0)
    assert not judge._validate_all_red(all_green_before_change, 3)[0]

    skipped = _result(
        _part(passed=2, total=2), _part(skipped=3, total=3), exit_code=0)
    assert not judge._validate_all_red(skipped, 3)[0]
    assert not judge._validate_all_green(skipped, 3)[0]

    not_collected = _result(
        _part(passed=2, total=2), _part(total=0), exit_code=0)
    assert not judge._validate_all_red(not_collected, 3)[0]
    assert not judge._validate_all_green(not_collected, 3)[0]


@pytest.mark.parametrize(
    "source",
    [
        """
def wrapper():
    def test_a(): assert True
    def test_b(): assert True
    def test_c(): assert True
""",
        """
class Helper:
    def test_a(self): assert True
    def test_b(self): assert True
    def test_c(self): assert True
""",
        """
def test_a(tmp_path): assert tmp_path
def test_b(monkeypatch): assert monkeypatch
def test_c(request): assert request
""",
        """
import pytest
def test_a(): pytest.skip('x'); assert False
def test_b(): pytest.skip('x'); assert False
def test_c(): pytest.skip('x'); assert False
""",
    ],
)
def test_hidden_test_linter_rejects_non_collectable_fixture_and_skip_cases(tmp_path, source):
    path = tmp_path / "hidden_tests.py"
    path.write_text(source, encoding="utf-8")
    result = judge.lint_hidden_test(path, required_count=3)
    assert not result["ok"]
    assert result["errors"]


def test_hidden_test_linter_allows_one_problem_across_multiple_modules(tmp_path):
    path = tmp_path / "hidden_tests.py"
    path.write_text(
        "from src.parser import parse\n"
        "from src.storage import Store\n\n"
        "def test_main_path(): assert parse(Store()) is not None\n"
        "def test_boundary(): assert parse(Store()) is not False\n"
        "def test_regression(): assert Store is not None\n",
        encoding="utf-8",
    )
    result = judge.lint_hidden_test(path, required_count=3)
    assert result["ok"]
    assert result["warnings"] == []


def _install_manual_flow_fakes(tmp_path, monkeypatch, run_results):
    harness_root = tmp_path / "harness"
    arena = tmp_path / "arena"
    (arena / "src").mkdir(parents=True)
    (arena / "tests").mkdir()
    (arena / "src" / "feature.py").write_text("VALUE = 0\n", encoding="utf-8")
    (arena / "tests" / "test_history.py").write_text("def test_history(): assert True\n", encoding="utf-8")
    (arena / "README.md").write_text("arena\n", encoding="utf-8")

    proposal = harness_root / "uploads" / "proposal"
    proposal.mkdir(parents=True)
    prompt = proposal / "next_prompt.md"
    hidden = proposal / "hidden_tests.py"
    prompt.write_text("把 VALUE 改为 1。\n", encoding="utf-8")
    hidden.write_text(
        "from src.feature import VALUE\n\n"
        "def test_a(): assert VALUE == 1\n"
        "def test_b(): assert VALUE > 0\n"
        "def test_c(): assert VALUE != 0\n",
        encoding="utf-8",
    )

    cfg = {
        "mock": False,
        "business_dir": "src",
        "history_tests_dir": "tests",
        "proposal_test_count": 3,
        "prompts_dir": "prompts",
        "hidden_tests_dir": "hidden_tests",
    }
    store = {
        "round": 1,
        "order": ["A", "B"],
        "survivors": ["A", "B"],
        "eliminated": [],
        "current_index": 0,
        "current_player": "A",
        "current_prompt_file": "round_1_initial.md",
        "status": "running",
        "phase": "proposing",
        "scores": {"A": 1, "B": 0},
        "pending_answer": None,
        "pending_proposal": {"prompt": str(prompt), "test": str(hidden)},
        "pending_proof": None,
        "last_result": None,
        "last_test_summary": None,
        "last_action_msg": None,
    }

    def load_state():
        return copy.deepcopy(store)

    def save_state(value):
        store.clear()
        store.update(copy.deepcopy(value))

    def copy_arena_to(dest, for_verify=True):
        ignore = shutil.ignore_patterns(".git") if for_verify else None
        shutil.copytree(arena, dest, dirs_exist_ok=True, ignore=ignore)

    monkeypatch.setattr(judge, "BASE_DIR", harness_root)
    monkeypatch.setattr(judge, "load_config", lambda: cfg)
    monkeypatch.setattr(judge, "load_state", load_state)
    monkeypatch.setattr(judge, "save_state", save_state)
    monkeypatch.setattr(judge, "resolve_path", lambda rel: Path(rel) if Path(rel).is_absolute() else harness_root / rel)
    monkeypatch.setattr(judge, "arena_path", lambda: arena)
    monkeypatch.setattr(judge, "log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(judge.repo_ops, "is_arena_ready", lambda: True)
    monkeypatch.setattr(judge.repo_ops, "copy_arena_to", copy_arena_to)
    if run_results is not None:
        results = iter(run_results)
        monkeypatch.setattr(judge, "run_pytest", lambda *args, **kwargs: copy.deepcopy(next(results)))
    return harness_root, arena, store


def test_manual_proof_flow_pauses_after_red_then_accepts_green(tmp_path, monkeypatch):
    harness_root, arena, store = _install_manual_flow_fakes(
        tmp_path, monkeypatch, [_red_result(), _green_result()])

    red = judge.verify_proposal()
    assert red["ok"] and red["result"] == "PASS" and red["gate"] == "RED"
    proof = store["pending_proof"]
    proof_root = Path(proof["root"])
    proof_repo = Path(proof["repo"])
    assert proof_repo.is_dir()
    assert Path(proof["prompt"]).read_text(encoding="utf-8") == "把 VALUE 改为 1。\n"
    assert not list(proof_repo.rglob("test_proposal_red_*.py"))
    assert store["phase"] == "proposing"

    (proof_repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    green = judge.verify_proposal()
    assert green["ok"] and green["result"] == "PASS" and green["gate"] == "GREEN"
    assert store["round"] == 2
    assert store["current_player"] == "B"
    assert store["phase"] == "answering"
    assert store["pending_proof"] is None
    assert not proof_root.exists()
    assert (harness_root / "hidden_tests" / "hidden_tests.py").is_file()
    assert (harness_root / "prompts" / "round_2_by_A.md").is_file()
    assert (arena / "src" / "feature.py").read_text(encoding="utf-8") == "VALUE = 0\n"


def test_manual_proof_rejects_changes_outside_business_dir(tmp_path, monkeypatch):
    _, _, store = _install_manual_flow_fakes(tmp_path, monkeypatch, [_red_result()])
    assert judge.verify_proposal()["gate"] == "RED"

    proof_repo = Path(store["pending_proof"]["repo"])
    (proof_repo / "README.md").write_text("tampered\n", encoding="utf-8")
    result = judge.verify_proposal()

    assert result["ok"] and result["result"] == "FAIL" and result["gate"] == "GREEN"
    assert "业务目录之外" in result["reason"]
    assert store["current_player"] == "B"
    assert "A" in store["eliminated"]


def test_manual_proof_flow_with_real_pytest_red_and_green(tmp_path, monkeypatch):
    _, arena, store = _install_manual_flow_fakes(tmp_path, monkeypatch, None)

    red = judge.verify_proposal()
    assert red["ok"] and red["gate"] == "RED" and red["hidden"]["passed"] == 0
    assert red["hidden"]["total"] == 3

    proof_repo = Path(store["pending_proof"]["repo"])
    (proof_repo / "src" / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    green = judge.verify_proposal()

    assert green["ok"] and green["gate"] == "GREEN"
    assert green["hidden"]["passed"] == green["hidden"]["total"] == 3
    assert store["current_player"] == "B"
    assert (arena / "src" / "feature.py").read_text(encoding="utf-8") == "VALUE = 0\n"


def test_test_content_snapshot_detects_changes_even_when_git_status_would_not(tmp_path, monkeypatch):
    arena = tmp_path / "arena"
    tests = arena / "tests"
    tests.mkdir(parents=True)
    target = tests / "test_history.py"
    target.write_text("def test_x(): assert 1\n", encoding="utf-8")
    monkeypatch.setattr(repo_ops, "load_config", lambda: {"history_tests_dir": "tests"})
    monkeypatch.setattr(repo_ops, "_safe_arena", lambda cfg=None: (arena, None))

    before = repo_ops.tests_status_map()
    target.write_text("def test_x(): assert 2\n", encoding="utf-8")
    after = repo_ops.tests_status_map()
    assert before != after


def test_answer_does_not_apply_material_when_pre_backup_fails(tmp_path, monkeypatch):
    arena = tmp_path / "arena"
    arena.mkdir()
    pending = tmp_path / "answer.py"
    pending.write_text("VALUE = 1\n", encoding="utf-8")
    hidden_dir = tmp_path / "hidden"
    hidden_dir.mkdir()
    (hidden_dir / "hidden_tests.py").write_text(
        "def test_a(): assert True\n"
        "def test_b(): assert True\n"
        "def test_c(): assert True\n",
        encoding="utf-8",
    )
    state = {
        "status": "running",
        "phase": "answering",
        "current_player": "A",
        "round": 1,
        "pending_answer": str(pending),
    }
    applied = {"called": False}

    monkeypatch.setattr(judge, "load_config", lambda: {
        "mock": False,
        "hidden_tests_dir": str(hidden_dir),
        "history_tests_dir": "tests",
        "proposal_test_count": 3,
    })
    monkeypatch.setattr(judge, "load_state", lambda: copy.deepcopy(state))
    monkeypatch.setattr(judge, "save_state", lambda value: None)
    monkeypatch.setattr(judge, "arena_path", lambda: arena)
    monkeypatch.setattr(judge.repo_ops, "is_arena_ready", lambda: True)
    monkeypatch.setattr(judge, "resolve_path", lambda value: Path(value))
    monkeypatch.setattr(judge, "backup_arena", lambda tag: None)

    def should_not_apply(path):
        applied["called"] = True
        raise AssertionError("material must not be applied without a backup")

    monkeypatch.setattr(judge, "apply_business_files", should_not_apply)
    result = judge.verify_answer()
    assert not result["ok"]
    assert "备份失败" in result["reason"]
    assert not applied["called"]
