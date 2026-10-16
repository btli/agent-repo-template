import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from agent_repo.cli import main


@pytest.mark.parametrize("command", ["unknown_command", "../x", "a.b", "__init__"])
def test_invalid_command_exits_two(command: str):
    assert main([command]) == 2


def test_command_module_without_main_exits_two(monkeypatch: pytest.MonkeyPatch):
    import types

    module = types.ModuleType("agent_repo.commands.no_main")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert main(["no_main"]) == 2


def test_nested_missing_command_dependency_is_not_reported_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
):
    def import_command(name: str):
        assert name == "agent_repo.commands.broken_import"
        raise ModuleNotFoundError("No module named 'nested_dependency'", name="nested_dependency")

    monkeypatch.setattr("agent_repo.cli.importlib.import_module", import_command)
    with pytest.raises(ModuleNotFoundError, match="nested_dependency"):
        main(["broken_import"])


def test_check_config_accepts_fixture(capsys: pytest.CaptureFixture[str]):
    assert main(["check_config", "tests/fixtures/agents"]) == 0
    assert "valid" in capsys.readouterr().out.lower()


def test_check_config_rejects_bad_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target = tmp_path / ".agents"
    target.mkdir()
    for path in Path("tests/fixtures/agents").iterdir():
        (target / path.name).write_bytes(path.read_bytes())
    (target / "config.yml").write_text("version: 1\nunknown: true\n")
    assert main(["check_config", str(target)]) == 1
    assert "unknown" in capsys.readouterr().err
