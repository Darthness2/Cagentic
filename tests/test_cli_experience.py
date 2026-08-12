from __future__ import annotations

import copy
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import click
import pytest

from cagentic import cli, command_utils, notes, service
from cagentic import gateway as gateway_module
from cagentic.agent import Agent
from cagentic.browser import BrowserBridge
from cagentic.completion import script as completion_script
from cagentic.engine import Message, QueryEngine, process_user_input
from cagentic.gateway import GATEWAY_COMMAND_NAMES, GATEWAY_COMMANDS, Gateway
from cagentic.ollama_client import OllamaClient
from cagentic.permissions import can_use_tool
from cagentic.prompt import (
    COMMAND_GROUPS,
    _attachment_completions,
    _safe_for_history,
    _toolbar_text,
)
from cagentic.state import AppState
from cagentic.tools import all_tool_schemas, t_write_file

_RUN_SPEC = importlib.util.spec_from_file_location(
    "cagentic_source_runner", Path(__file__).resolve().parents[1] / "run.py"
)
assert _RUN_SPEC is not None and _RUN_SPEC.loader is not None
source_runner = importlib.util.module_from_spec(_RUN_SPEC)
_RUN_SPEC.loader.exec_module(source_runner)


class _NullClient:
    pass


class _CommandClient:
    def list_models(self) -> list[str]:
        return ["test-model"]


class _ReplyClient:
    def chat(self, **_kwargs) -> dict:
        return {"role": "assistant", "content": "preview complete"}


class _MCPStub:
    def names(self) -> list[str]:
        return []

    def list_tools(self, _server: str) -> list[dict]:
        return []


class _BrowserStub:
    error = None
    port = 8765
    token = "stub-token"

    def is_connected(self) -> bool:
        return False

    def auth_failing(self, window: float = 90.0) -> bool:
        return False


class _ScriptedPrompt:
    status_note = None
    backend = "test"

    def __init__(self, *lines: str) -> None:
        self._lines = iter(lines)
        self.workspace_provider = None
        self.context_provider = None

    def set_workspace_provider(self, provider) -> None:
        self.workspace_provider = provider

    def set_context_provider(self, provider) -> None:
        self.context_provider = provider

    def ask(self, _prefix: str) -> str:
        return next(self._lines)


_COMMAND_PATHS = [
    ("chat",),
    ("run",),
    ("doctor",),
    ("sessions",),
    ("search",),
    ("context",),
    ("compact",),
    ("setup",),
    ("login",),
    ("logout",),
    ("completion",),
    ("serve",),
    ("service",),
    ("service", "install"),
    ("service", "uninstall"),
    ("config",),
    ("config", "reset"),
]


def _click_command(path: tuple[str, ...]) -> click.Command:
    command: click.Command = cli.cli
    for part in path:
        assert isinstance(command, click.Group)
        command = command.commands[part]
    return command


def _agent(tmp_path, monkeypatch) -> Agent:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return Agent(_NullClient(), "test-model", tmp_path, stream=True)


def _gateway(tmp_path, monkeypatch, *, client=None, cfg=None) -> Gateway:
    monkeypatch.setattr(gateway_module, "_warm_model_cache", lambda _cfg: None)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    agent = Agent(client or _NullClient(), "test-model", tmp_path, stream=True)
    return Gateway(agent, cfg if cfg is not None else {}, port=18990)


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--port", "70000"],
        ["serve", "--port", "0"],
        ["doctor", "unexpected"],
        ["--port", "8700"],
        ["--threshold", "0.5"],
        ["--format", "json"],
        ["context", "session", "--threshold", "1.1"],
        ["run", "hello", "--temperature", "-0.1"],
        ["run", "hello", "--model", "openai:"],
        ["run", "hello", "--host", "localhost:not-a-port"],
        ["run"],
        ["login", "openai"],
    ],
)
def test_cli_rejects_ambiguous_or_invalid_arguments(argv, capsys):
    assert cli.main(argv) == 2
    assert "traceback" not in capsys.readouterr().err.casefold()


def test_cli_help_is_grouped_and_includes_examples(capsys):
    assert cli.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "Commands:" in output
    assert "chat" in output
    assert "run" in output
    assert "doctor" in output
    assert "serve" in output
    assert "-p, --prompt" in output
    assert "--doctor" in output
    assert "--serve" in output
    assert "Examples:" in output


@pytest.mark.parametrize("path", _COMMAND_PATHS)
def test_every_command_supports_short_help(path, capsys):
    assert cli.main([*path, "-h"]) == 0
    assert "Usage:" in capsys.readouterr().out


def test_root_without_a_mode_starts_the_interactive_repl(monkeypatch):
    seen: list[cli.RuntimeOptions] = []
    monkeypatch.setattr(cli, "_run_runtime", lambda options: seen.append(options) or 0)
    assert cli.main([]) == 0
    assert seen[0].mode == "chat"
    assert seen[0].prompt is None


def test_double_dash_allows_a_positional_that_starts_with_a_hyphen(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cli, "_search_sessions", lambda query: seen.append(query) or [])
    assert cli.main(["search", "--", "-needle"]) == 0
    assert seen == ["-needle"]


def test_unknown_command_suggests_the_nearest_valid_command(capsys):
    assert cli.main(["doctro"]) == 2
    assert "Did you mean 'doctor'?" in capsys.readouterr().err


def test_usage_errors_follow_the_requested_json_format(capsys):
    assert cli.main(["setup", "--format", "JSON"]) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 2
    assert "--interactive" in payload["error"]
    assert captured.err == ""
    assert "\x1b" not in captured.out


def test_runtime_failures_remain_machine_readable(monkeypatch, capsys):
    def fail(_options):
        cli.ui.error("provider unavailable; start it and retry")
        return 1

    monkeypatch.setattr(cli, "_run_runtime", fail)
    assert cli.main(["-p", "hello", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert any("start it and retry" in line for line in payload["errors"])
    assert captured.err == ""


def test_unexpected_errors_are_clean_unless_debug_is_enabled(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.sessions,
        "list_all",
        lambda: (_ for _ in ()).throw(RuntimeError("broken store")),
    )
    assert cli.main(["--sessions"]) == 1
    captured = capsys.readouterr()
    assert "broken store" in captured.err
    assert "Traceback (most recent call last)" not in captured.out + captured.err

    with pytest.raises(RuntimeError, match="broken store"):
        cli.main(["--debug", "--sessions"])


@pytest.mark.parametrize(
    "path",
    [
        ("run",),
        ("doctor",),
        ("sessions",),
        ("search",),
        ("context",),
        ("compact",),
        ("setup",),
        ("login",),
        ("logout",),
        ("serve",),
        ("service", "install"),
        ("service", "uninstall"),
        ("config", "reset"),
    ],
)
def test_scriptable_commands_offer_json(path):
    options = {
        option
        for parameter in _click_command(path).params
        if isinstance(parameter, click.Option)
        for option in parameter.opts
    }
    assert "--format" in options


@pytest.mark.parametrize(
    "path",
    [
        ("chat",),
        ("run",),
        ("compact",),
        ("setup",),
        ("login",),
        ("logout",),
        ("serve",),
        ("service", "install"),
        ("service", "uninstall"),
        ("config", "reset"),
    ],
)
def test_mutating_commands_offer_dry_run(path):
    options = {
        option
        for parameter in _click_command(path).params
        if isinstance(parameter, click.Option)
        for option in parameter.opts
    }
    assert "--dry-run" in options


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["chat", "--help"],
        ["run", "--help"],
        ["doctor", "--help"],
        ["setup", "--help"],
        ["login", "--help"],
        ["logout", "--help"],
        ["completion", "--help"],
        ["sessions", "--help"],
        ["search", "--help"],
        ["context", "--help"],
        ["compact", "--help"],
        ["serve", "--help"],
        ["service", "--help"],
        ["service", "install", "--help"],
        ["service", "uninstall", "--help"],
        ["config", "--help"],
        ["config", "reset", "--help"],
    ],
)
def test_every_documented_command_has_help(argv, capsys):
    assert cli.main(argv) == 0
    assert "Usage:" in capsys.readouterr().out


def test_every_cli_option_has_help_text():
    def commands(command: click.Command):
        yield command
        if isinstance(command, click.Group):
            for child in command.commands.values():
                yield from commands(child)

    for command in commands(cli.cli):
        for option in command.params:
            if isinstance(option, click.Option):
                assert option.help and option.help.strip(), (command.name, option.opts)


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_shell_completion_covers_every_long_cli_option(shell):
    output = completion_script(shell)
    assert "_CAGENTIC_COMPLETE" in output
    assert "cagentic" in output
    if shell == "bash":
        assert "complete" in output
    elif shell == "zsh":
        assert "#compdef" in output
    else:
        assert "complete --no-files --command cagentic" in output


def test_sessions_json_is_machine_readable(monkeypatch, capsys):
    rows = [{"id": "abc", "title": "Example", "updated_at": 1}]
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.sessions, "list_all", lambda: rows)
    assert cli.main(["--sessions", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "sessions": rows}


def test_cli_service_modes_normalize_failure_exit_codes(monkeypatch):
    monkeypatch.setattr(service, "install", lambda: 7)
    monkeypatch.setattr(service, "uninstall", lambda: 8)
    assert cli.main(["--install-service"]) == 1
    assert cli.main(["--uninstall-service"]) == 1


def test_reset_config_removes_only_the_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = cli.config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text("{}")
    note = path.parent / "keep-me.txt"
    note.write_text("keep")
    assert cli.main(["--reset-config"]) == 0
    assert not path.exists()
    assert note.read_text() == "keep"


def test_config_reset_dry_run_preserves_the_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = cli.config.config_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"model": "keep"}')

    assert cli.main(["--reset-config", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert path.read_text() == '{"model": "keep"}'


def test_setup_dry_run_does_not_save(monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(
        cli.config,
        "save",
        lambda _cfg: pytest.fail("dry-run setup must not save"),
    )

    assert cli.main(["setup", "--model", "test-model", "--dry-run", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["changes"] == ["model"]


def test_public_setup_flag_runs_the_guided_wizard(tmp_path, monkeypatch):
    answers = iter(["test-model", "Alex", str(tmp_path), "n"])
    saved: list[dict] = []
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.config, "save", lambda cfg: saved.append(copy.deepcopy(cfg)))
    monkeypatch.setattr(cli.ui, "input_prompt", lambda *_args, **_kwargs: next(answers))

    assert cli.main(["--setup"]) == 0
    assert saved == [
        {
            "model": "test-model",
            "user_name": "Alex",
            "gateway": {"workspace_roots": [str(tmp_path)]},
        }
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["--doctor", "--model", "test-model"],
        ["--sessions", "--name", "Alex"],
        ["--setup", "--host", "http://localhost:11434"],
        ["--search", "needle", "--dry-run"],
        ["--context", "session", "--dry-run"],
    ],
)
def test_public_modes_reject_options_that_would_be_ignored(argv, capsys):
    assert cli.main(argv) == 2
    error = capsys.readouterr().err
    assert "cannot be used" in error or "has no effect" in error


def test_login_dry_run_needs_no_secret_or_input(monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(
        cli.config,
        "save",
        lambda _cfg: pytest.fail("dry-run login must not save"),
    )
    monkeypatch.setattr(
        click,
        "get_text_stream",
        lambda _name: pytest.fail("dry-run login must not read stdin"),
    )

    assert cli.main(["--login", "openai", "--dry-run", "--json"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["dry_run"] is True
    assert "dry-run-placeholder" not in output


def test_compact_dry_run_does_not_save_or_mutate_loaded_data(monkeypatch, capsys):
    loaded = {
        "id": "abc",
        "model": "test-model",
        "messages": [
            {"role": "user", "content": f"message {index} " + "x" * 1000} for index in range(12)
        ],
    }
    before = copy.deepcopy(loaded)
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.sessions, "list_all", lambda: [{"id": "abc"}])
    monkeypatch.setattr(cli.sessions, "load", lambda _session_id: loaded)
    monkeypatch.setattr(
        cli.sessions,
        "save",
        lambda _data: pytest.fail("dry-run compaction must not save"),
    )

    assert cli.main(["--compact", "abc", "--threshold", "0.01", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert loaded == before


def test_compact_save_failure_reports_possible_partial_state(monkeypatch, capsys):
    loaded = {
        "id": "abc",
        "model": "test-model",
        "messages": [
            {"role": "user", "content": f"message {index} " + "x" * 1000} for index in range(12)
        ],
    }
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.sessions, "list_all", lambda: [{"id": "abc"}])
    monkeypatch.setattr(cli.sessions, "load", lambda _session_id: loaded)
    monkeypatch.setattr(
        cli.sessions,
        "save",
        lambda _data: (_ for _ in ()).throw(OSError("index unavailable")),
    )

    assert cli.main(["--compact", "abc", "--threshold", "0.01", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "file and index may differ" in payload["error"]


def test_service_dry_run_never_invokes_the_service_manager(monkeypatch, capsys):
    monkeypatch.setattr(
        service,
        "install",
        lambda: pytest.fail("dry-run install must not invoke the service manager"),
    )
    assert cli.main(["--install-service", "--dry-run", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["action"] == "install"


def test_service_definitions_use_the_public_serve_flag():
    assert "<string>--serve</string>" in service._LAUNCHD_PLIST
    assert " -m cagentic --serve" in service._SYSTEMD_UNIT


def test_dry_run_permission_cannot_be_bypassed_by_yolo_or_cached_approval(tmp_path):
    state = AppState(
        workspace=tmp_path,
        home=tmp_path,
        yolo=True,
        dry_run=True,
        plan_mode=False,
    )
    state.permissions["write_file"] = "always"
    allowed, reason = can_use_tool("write_file", {"path": "x"}, state)
    assert allowed is False
    assert "dry run" in reason


def test_dry_run_engine_uses_ephemeral_state_and_skips_transcripts(tmp_path, monkeypatch):
    config_root = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_root))
    monkeypatch.setattr(
        "cagentic.engine.record_transcript",
        lambda *_args, **_kwargs: pytest.fail("dry-run turns must not append transcripts"),
    )
    state = AppState(workspace=tmp_path, home=tmp_path, dry_run=True, plan_mode=True)
    engine = QueryEngine(
        _ReplyClient(),
        state,
        "test-model",
        session_id="dry-run-session",
        stream=False,
    )

    events = list(engine.submit_message("preview this"))

    assert any(event.kind == "done" for event in events)
    assert not config_root.exists()
    assert config_root not in engine.task_graph.root.parents


def test_version_mode_exits_cleanly(capsys):
    assert cli.main(["--version"]) == 0
    assert capsys.readouterr().out == f"cagentic {cli.__version__}\n"


def test_context_json_errors_are_machine_readable(monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(cli.sessions, "list_all", lambda: [])
    assert cli.main(["--context", "missing", "--json"]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": "no session matching 'missing'",
    }


def test_noninteractive_setup_requires_explicit_values(monkeypatch, capsys):
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(
        cli.config,
        "save",
        lambda _cfg: pytest.fail("setup without values must not save config"),
    )
    assert cli.main(["setup"]) == 2
    assert "--interactive" in capsys.readouterr().err


def test_setup_save_failure_returns_a_clean_exit(monkeypatch):
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(
        cli.config,
        "save",
        lambda _cfg: (_ for _ in ()).throw(OSError("disk full")),
    )
    assert cli.main(["setup", "--model", "test-model"]) == 1


def test_top_level_login_saves_a_hidden_key_before_provider_startup(monkeypatch):
    cfg: dict = {}
    saved: list[dict] = []
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(cli.config, "save", lambda data: saved.append(copy.deepcopy(data)))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "sk-hidden")
    monkeypatch.setattr(
        cli,
        "_build_client",
        lambda *_args, **_kwargs: pytest.fail("credential mode must not start a provider"),
    )

    assert cli.main(["--login", "openai"]) == 0
    assert saved[-1]["providers"]["openai"]["api_key"] == "sk-hidden"


def test_top_level_logout_removes_key_and_warns_about_environment(monkeypatch):
    cfg = {"providers": {"anthropic": {"api_key": "saved-key"}}}
    saved: list[dict] = []
    warnings: list[str] = []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment-key")
    monkeypatch.setattr(cli.config, "load", lambda: cfg)
    monkeypatch.setattr(cli.config, "save", lambda data: saved.append(copy.deepcopy(data)))
    monkeypatch.setattr(cli.ui, "warn", warnings.append)

    assert cli.main(["--logout", "anthropic"]) == 0
    assert saved[-1]["providers"]["anthropic"]["api_key"] is None
    assert any("ANTHROPIC_API_KEY" in warning for warning in warnings)


def test_github_logout_warns_for_either_supported_environment_variable(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setenv("GH_TOKEN", "environment-key")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cli.ui, "warn", warnings.append)
    cli._warn_credential_environment("github", login=False)
    assert any("GH_TOKEN" in warning for warning in warnings)


@pytest.mark.parametrize("secret", ["", "   "])
def test_top_level_login_rejects_empty_keys_without_saving(secret, monkeypatch):
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr("getpass.getpass", lambda _prompt: secret)
    monkeypatch.setattr(
        cli.config,
        "save",
        lambda _cfg: pytest.fail("an empty credential must not be saved"),
    )
    assert cli.main(["--login", "github"]) == 1


def test_invalid_workspace_fails_before_provider_startup(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(
        cli,
        "_build_client",
        lambda *_args, **_kwargs: pytest.fail("provider should not start"),
    )
    missing = tmp_path / "does-not-exist"
    assert cli.main(["-p", "hello", "--model", "test", "-C", str(missing)]) == 2


def test_name_save_failure_stops_before_provider_startup(monkeypatch):
    monkeypatch.setattr(cli.config, "load", lambda: {})
    monkeypatch.setattr(
        cli.config,
        "save",
        lambda _cfg: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(
        cli,
        "_build_client",
        lambda *_args, **_kwargs: pytest.fail("provider should not start after save failure"),
    )
    assert cli.main(["--name", "Alice", "--model", "test-model"]) == 1


def test_session_prefixes_must_be_unambiguous():
    listed = [{"id": "abc111"}, {"id": "abc222"}, {"id": "def333"}]
    assert cli._resolve_session_arg("def", listed) == listed[2]
    assert cli._resolve_session_arg("abc", listed) is None
    assert "matches 2 sessions" in cli._session_miss("abc", listed)


def test_created_file_undo_removes_the_file(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    target = tmp_path / "new.txt"
    assert t_write_file({"path": "new.txt", "content": "hello\n"}, agent.ctx).startswith("OK:")
    assert agent.state.edit_history[-1]["op"] == "create"

    prompt = _ScriptedPrompt("/undo", "/quit")
    monkeypatch.setattr(cli, "Prompt", lambda: prompt)
    assert cli.repl(agent, {}) == 0
    assert not target.exists()


def test_undo_refuses_to_destroy_newer_user_changes(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    target = tmp_path / "new.txt"
    assert t_write_file({"path": "new.txt", "content": "agent\n"}, agent.ctx).startswith("OK:")
    target.write_text("user changed this\n")

    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/undo", "/quit"))
    assert cli.repl(agent, {}) == 0
    assert target.read_text() == "user changed this\n"
    assert len(agent.state.edit_history) == 1


def test_clear_persists_the_empty_conversation(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    agent.engine.messages.extend(
        [
            {"role": "user", "content": "keep this only until clear"},
            {"role": "assistant", "content": "okay"},
        ]
    )
    saved: list[dict] = []
    monkeypatch.setattr(cli.sessions, "save", lambda data: saved.append(dict(data)))
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/clear", "/quit"))

    assert cli.repl(agent, {}) == 0
    assert saved
    assert saved[-1]["messages"] == []


def test_tools_command_uses_grouped_renderer(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    calls: list[Agent] = []
    monkeypatch.setattr(cli, "print_tools", calls.append)
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/tools", "/quit"))
    assert cli.repl(agent, {}) == 0
    assert calls == [agent]


def test_login_uses_hidden_prompt_and_rejects_inline_secrets(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    cfg: dict = {}
    saved: list[dict] = []
    monkeypatch.setattr(cli.config, "save", lambda data: saved.append(dict(data)))
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/login openai", "/quit"))
    monkeypatch.setattr("getpass.getpass", lambda _prompt: "sk-hidden")
    assert cli.repl(agent, cfg) == 0
    assert cfg["providers"]["openai"]["api_key"] == "sk-hidden"
    assert saved

    saved.clear()
    monkeypatch.setattr(
        cli,
        "Prompt",
        lambda: _ScriptedPrompt("/login openai sk-must-not-enter-history", "/quit"),
    )
    assert cli.repl(agent, cfg) == 0
    assert not saved


def test_logout_clears_the_active_cloud_clients_credential(tmp_path, monkeypatch):
    from cagentic.openai_client import OpenAIClient

    client = OpenAIClient("sk-live")
    agent = Agent(client, "gpt-test", tmp_path, stream=True)
    agent.state.update(active_model_spec="openai:gpt-test")
    cfg = {"providers": {"openai": {"api_key": "sk-live"}}}
    monkeypatch.setattr(cli.config, "save", lambda _cfg: None)
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/logout openai", "/quit"))
    assert cli.repl(agent, cfg) == 0
    assert client.api_key == ""
    assert "Authorization" not in client._session.headers


def test_secret_commands_are_excluded_from_input_history():
    assert _safe_for_history("/login openai") is False
    assert _safe_for_history("/login openai sk-secret") is False
    assert _safe_for_history("/set gateway.token secret") is False
    assert _safe_for_history("/set ollama.num_ctx 8192") is True


def test_attachment_completion_uses_the_live_workspace_and_quotes_spaces(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "release notes.md").write_text("ship it\n")
    (tmp_path / "config(v2).json").write_text("{}\n")
    (tmp_path / "résumé.md").write_text("hello\n")
    (tmp_path / ".secret").write_text("hidden\n")

    rows = _attachment_completions("summarize @", tmp_path)
    replacements = {display: replacement for replacement, display, _detail, _length in rows}
    assert replacements[f"src{os.sep}"] == f"src{os.sep}"
    assert replacements["release notes.md"] == '"release notes.md"'
    assert replacements["config(v2).json"] == '"config(v2).json"'
    assert replacements["résumé.md"] == "résumé.md"
    assert ".secret" not in replacements
    assert _attachment_completions("email me@example", tmp_path) == []


@pytest.mark.parametrize("columns", [32, 48, 72, 104])
def test_live_prompt_toolbar_prioritizes_state_and_fits(columns):
    toolbar = _toolbar_text(
        {
            "model": "openai:gpt-4o",
            "workspace": "/Users/example/a/long/project/path",
            "mode": "plan",
            "approval": "ask changes",
            "tools": "tools on",
        },
        columns,
    )

    assert len(toolbar) <= columns
    assert "plan" in toolbar
    assert "ask" in toolbar
    assert "/" in toolbar
    assert "\x1b" not in toolbar


def test_repl_toolbar_context_tracks_live_workspace_and_mode(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    workspace = tmp_path / "next workspace"
    workspace.mkdir()
    prompt = _ScriptedPrompt(f"/cd {workspace}", "/plan on", "/quit")
    monkeypatch.setattr(cli, "Prompt", lambda: prompt)

    assert cli.repl(agent, {}) == 0
    assert prompt.context_provider is not None
    context = prompt.context_provider()
    assert context["workspace"] == workspace
    assert context["mode"] == "plan"
    assert context["approval"] == "ask changes"


def test_attachments_support_home_paths_quoted_spaces_and_line_ranges(tmp_path):
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    (workspace / "local.txt").write_text("local one\nlocal two\n")
    (workspace / "résumé.md").write_text("unicode path\n")
    (home / "project notes.md").write_text("home one\nhome two\nhome three\n")

    message = process_user_input(
        'compare @local.txt:2, @résumé.md, and @"~/project notes.md":1-2',
        workspace=workspace,
        home=home,
    )

    assert message["_attachment_count"] == 3
    assert "local two" in message["content"]
    assert "local one" not in message["content"]
    assert "home one" in message["content"]
    assert "home two" in message["content"]
    assert "home three" not in message["content"]
    assert "unicode path" in message["content"]


def test_agent_marks_error_only_turn_as_failed(capsys):
    agent = Agent.__new__(Agent)
    agent.engine = SimpleNamespace(
        messages=[],
        submit_message=lambda _prompt: iter([Message("error", {"text": "provider failed"})]),
    )
    agent.on_turn_complete = None
    agent.last_turn_failed = False

    assert agent.turn("hello") == ""
    assert agent.last_turn_failed is True
    assert "provider failed" in capsys.readouterr().err


def test_invalid_local_server_ports_return_clean_errors():
    gateway = Gateway.__new__(Gateway)
    gateway._server = None
    gateway.requested_port = 70000
    assert gateway.start() is False
    assert gateway.error == "invalid gateway port 70000; expected 1-65535"

    browser = BrowserBridge(port=-1)
    assert browser.start() is False
    assert browser.error == "invalid browser bridge port -1; expected 1-65535"


def test_invalid_config_ports_fall_back_cleanly(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(cli.ui, "warn", warnings.append)
    assert cli._configured_port({"gateway": {"port": True}}, "gateway", 8700) == 8700
    assert cli._configured_port({"browser": "broken"}, "browser", 8765) == 8765
    assert len(warnings) == 2


def test_string_booleans_never_enable_sensitive_settings(monkeypatch):
    warnings: list[str] = []
    monkeypatch.setattr(cli.ui, "warn", warnings.append)
    cfg = {"yolo": "false", "insecure_ssl": "false", "ollama": {"stream": "false"}}
    assert cli._configured_bool(cfg, "yolo", False) is False
    assert cli._configured_bool(cfg, "insecure_ssl", False) is False
    assert cli._configured_bool(cfg, "ollama.stream", True) is True
    assert len(warnings) == 3


def test_malformed_nested_config_does_not_break_command_surfaces(tmp_path, monkeypatch):
    from cagentic.providers import build_client

    cfg = {
        "host": 123,
        "ollama": "broken",
        "gateway": "broken",
        "browser": "broken",
        "proactive": "broken",
        "mcp": "broken",
        "system_prompt": 123,
    }
    client = build_client(cfg, "ollama")
    assert client.host == "http://localhost:11434"
    assert client.num_ctx == 8192
    gateway = _gateway(tmp_path, monkeypatch, client=_CommandClient(), cfg=cfg)
    assert gateway.handle_cmd("diag")["ok"] is True
    assert gateway.handle_cmd("mcp") == {"ok": True, "text": "no MCP servers configured"}


def test_failed_browser_command_can_be_retried(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    starts: list[int] = []

    class FailedBrowser:
        error = "port unavailable"

        def __init__(self, *, port, site_rules=None):
            self.port = port
            self.site_rules = site_rules

        def start(self):
            starts.append(self.port)
            return False

    monkeypatch.setattr("cagentic.browser.BrowserBridge", FailedBrowser)
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/browser", "/browser", "/quit"))
    assert cli.repl(agent, {}) == 0
    assert starts == [8765, 8765]
    assert agent.state.browser is None


def test_diag_and_host_errors_return_to_the_prompt(tmp_path, monkeypatch):
    client = OllamaClient("http://localhost:11434")
    agent = Agent(client, "test-model", tmp_path, stream=True)

    def fail_status(_model):
        raise RuntimeError("status unavailable")

    monkeypatch.setattr(client, "model_vram_status", fail_status)
    monkeypatch.setattr(
        cli,
        "Prompt",
        lambda: _ScriptedPrompt("/diag", "/host localhost:not-a-port", "/quit"),
    )
    assert cli.repl(agent, {}) == 0
    assert client.host == "http://localhost:11434"


def test_config_redaction_covers_gateway_and_nested_secrets():
    raw = {
        "gateway": {"token": "gateway-secret"},
        "mcp": {"servers": {"x": {"env": {"CUSTOM_ACCESS_TOKEN": "nested-secret"}}}},
        "ollama": {"num_ctx": 8192},
    }
    safe = cli.config.redact_secrets(raw)
    assert safe["gateway"]["token"] != "gateway-secret"
    assert safe["mcp"]["servers"]["x"]["env"]["CUSTOM_ACCESS_TOKEN"] != "nested-secret"
    assert safe["ollama"]["num_ctx"] == 8192
    assert raw["gateway"]["token"] == "gateway-secret"


def test_gateway_cli_tools_notes_clear_and_group_validation(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path, monkeypatch)

    monkeypatch.setattr(gateway_module._config, "save", lambda _cfg: None)
    secret_result = gateway.handle_cmd("set", "gateway.token", "never-print-this")
    assert secret_result["ok"] is True
    assert "never-print-this" not in secret_result["text"]

    tools_result = gateway.handle_cmd("tools")
    assert tools_result["ok"] is True
    assert "read_file" in tools_result["text"]

    notes.write("release-checklist", "Ship it carefully")
    notes_result = gateway.handle_cmd("notes")
    assert notes_result["ok"] is True
    assert "release-checklist" in notes_result["text"]

    gateway.engine.messages.append({"role": "user", "content": "temporary"})
    saved: list[dict] = []
    monkeypatch.setattr(
        gateway_module.sessions,
        "save",
        lambda data: saved.append({**data, "messages": list(data.get("messages", []))}),
    )
    clear_result = gateway.handle_cmd("clear")
    assert clear_result["ok"] is True
    assert clear_result["current"]["messages"] == []
    assert gateway.engine.messages[0]["role"] == "system"
    assert saved[-1]["messages"] == []

    before = gateway.agent.state.tool_groups
    group_result = gateway.handle_cmd("groups", "enable", "does-not-exist")
    assert group_result == {"ok": False, "text": "unknown tool group 'does-not-exist'"}
    assert gateway.agent.state.tool_groups == before


def test_gateway_host_uses_canonical_config_key(tmp_path, monkeypatch):
    cfg: dict = {}
    gateway = _gateway(
        tmp_path,
        monkeypatch,
        client=OllamaClient("http://localhost:11434"),
        cfg=cfg,
    )
    result = gateway.handle_cmd("host", "localhost:1234")
    assert result["ok"] is True
    assert cfg["host"] == "http://localhost:1234"
    assert "ollama" not in cfg


def test_gateway_model_switch_restores_per_model_tool_capability(tmp_path, monkeypatch):
    cfg = {"models": {"openai:gpt.5": {"tools_supported": False}}}
    gateway = _gateway(tmp_path, monkeypatch, cfg=cfg)
    monkeypatch.setattr(gateway_module, "_build_client", lambda *_args: _NullClient())

    assert gateway._activate_model("openai:gpt.5") is None
    assert gateway.agent.state.active_model_spec == "openai:gpt.5"
    assert gateway.agent.state.tools_enabled is False


def test_show_widget_emits_a_widget_event_to_the_web_ui(tmp_path, monkeypatch):
    """show_widget is rendered by the gateway web UI (app.js draws it as a HUD
    window), so the tool has to reach the SSE stream — it outlived the iOS
    client it was originally written for."""
    gateway = _gateway(tmp_path, monkeypatch)
    emitted: list[tuple[str, dict]] = []
    gateway._active_emit = lambda kind, data: emitted.append((kind, data))

    schema_names = {
        schema["function"]["name"]
        for schema in all_tool_schemas(gateway.agent.state.tool_groups, compact=False)
    }
    assert "show_widget" in schema_names

    events = list(
        gateway.engine.executor._run_one(
            (
                "show_widget",
                {"type": "stats", "title": "Build", "data": {"items": []}},
                "assistant",
            )
        )
    )
    result = next(event for event in events if event.kind == "tool_result")
    assert result.data["ok"] is True
    assert emitted == [("widget", {"type": "stats", "title": "Build", "data": {"items": []}})]


def test_no_phone_tools_remain_in_the_registry():
    """The iOS backend is gone; nothing should re-introduce a phone tool
    without also rebuilding the client bridge that used to execute it."""
    from cagentic.tools import TOOL_GROUPS, TOOLS

    assert not [name for name in TOOLS if name.startswith("phone_")]
    assert "phone" not in TOOL_GROUPS


@pytest.mark.parametrize(
    ("content", "args", "message"),
    [
        ("[]", {"op": "get"}, "root must be a JSON object"),
        ('{"cells": "bad"}', {"op": "get"}, "cells' must be a list"),
        ('{"cells": []}', {"op": "replace", "source": "x"}, "cell_index is required"),
        (
            '{"cells": []}',
            {"op": "insert", "cell_index": "nope", "source": "x"},
            "cell_index must be an integer",
        ),
    ],
)
def test_notebook_edit_reports_malformed_input(tmp_path, monkeypatch, content, args, message):
    from cagentic.coding_tools import t_notebook_edit

    agent = _agent(tmp_path, monkeypatch)
    (tmp_path / "bad.ipynb").write_text(content)
    result = t_notebook_edit({"path": "bad.ipynb", **args}, agent.ctx)
    assert result.startswith("ERROR:")
    assert message in result


def test_tool_support_keys_allow_provider_and_dotted_model_names():
    cfg = {"models": {"openai:gpt.5": {"tools_supported": False}}}
    assert cli._tools_supported(cfg, "openai:gpt.5") is False
    cli._remember_tools_unsupported(cfg, "anthropic:claude.5")
    assert cfg["models"]["anthropic:claude.5"]["tools_supported"] is False


def test_source_runner_install_only_does_not_launch(monkeypatch):
    monkeypatch.setattr(source_runner, "_in_project_venv", lambda: True)
    monkeypatch.setattr(source_runner, "_install_deps", lambda: True)
    monkeypatch.setattr(
        source_runner,
        "_run_cagentic",
        lambda _args: pytest.fail("--install should not launch Cagentic"),
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--install"])
    assert source_runner.main() == 0


@pytest.mark.parametrize(
    ("flag", "expected"),
    [([], None), (["--yolo"], True), (["--no-yolo"], False)],
)
def test_cli_yolo_has_an_explicit_safe_override(flag, expected, monkeypatch):
    seen: list[cli.RuntimeOptions] = []
    monkeypatch.setattr(cli, "_run_runtime", lambda options: seen.append(options) or 0)
    assert cli.main(flag) == 0
    assert seen[0].yolo is expected


def test_command_config_values_are_typed_and_validated():
    assert command_utils.parse_config_value("false") is False
    assert command_utils.parse_config_value("8192") == 8192
    assert command_utils.parse_config_value('["files", "web"]') == ["files", "web"]
    assert command_utils.parse_config_value("qwen2.5:7b") == "qwen2.5:7b"
    assert command_utils.validate_config_value("temperature", "hot") is not None
    assert command_utils.validate_config_value("gateway.port", 70000) is not None
    assert command_utils.validate_config_value("proactive.interval", 29) is not None
    assert command_utils.validate_config_value("proactive.interval", 60) is None
    assert command_utils.validate_config_key("ollama..stream") is not None


def test_repl_accepts_workspace_and_note_names_with_spaces(tmp_path, monkeypatch, capsys):
    agent = _agent(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace with spaces"
    workspace.mkdir()
    notes.write("release checklist", "Ship carefully")
    monkeypatch.setattr(
        cli,
        "Prompt",
        lambda: _ScriptedPrompt(f"/cd {workspace}", "/note release checklist", "/quit"),
    )

    assert cli.repl(agent, {}) == 0
    assert agent.state.workspace == workspace
    assert "Ship carefully" in capsys.readouterr().out


def test_repl_rejects_an_unparseable_reminder_due_time(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli._reminders,
        "add",
        lambda *_args, **_kwargs: pytest.fail("invalid due time must not be saved"),
    )
    monkeypatch.setattr(
        cli,
        "Prompt",
        lambda: _ScriptedPrompt("/remind add call mom @ someday-ish", "/quit"),
    )
    assert cli.repl(agent, {}) == 0


def test_new_title_belongs_only_to_the_new_session(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    agent.engine.messages.extend(
        [
            {"role": "user", "content": "old conversation"},
            {"role": "assistant", "content": "old reply"},
        ]
    )
    saved: list[dict] = []
    monkeypatch.setattr(cli.sessions, "save", lambda data: saved.append(copy.deepcopy(data)))
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/new New Project", "/quit"))

    assert cli.repl(agent, {}) == 0
    assert saved
    assert saved[0].get("title") != "New Project"


def test_resume_saves_current_session_before_switching_models(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    agent.engine.messages.extend(
        [
            {"role": "user", "content": "current conversation"},
            {"role": "assistant", "content": "current reply"},
        ]
    )
    target = {
        "id": "target-session",
        "title": "Target",
        "model": "new-model",
        "updated_at": 1,
        "turns": 1,
    }
    loaded = {**target, "messages": [{"role": "user", "content": "target turn"}]}
    saved: list[dict] = []
    monkeypatch.setattr(cli.sessions, "list_all", lambda: [target])
    monkeypatch.setattr(cli.sessions, "load", lambda _session_id: copy.deepcopy(loaded))
    monkeypatch.setattr(cli.sessions, "save", lambda data: saved.append(copy.deepcopy(data)))

    def activate(a, _cfg, model):
        assert saved and saved[-1]["model"] == "test-model"
        a.model = model
        a.state.update(active_model_spec=model)

    monkeypatch.setattr(cli, "_activate_model", activate)
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/resume target-session", "/quit"))

    assert cli.repl(agent, {}) == 0
    assert saved[0]["model"] == "test-model"


def test_retry_restores_the_history_before_the_previous_turn(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    histories: list[list[dict]] = []

    def turn(text: str, typeahead=None) -> str:
        histories.append(copy.deepcopy(agent.messages))
        agent.engine.messages.extend(
            [
                {"role": "user", "content": text},
                {"role": "assistant", "content": f"reply {len(histories)}"},
            ]
        )
        return "ok"

    monkeypatch.setattr(agent, "turn", turn)
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("hello", "/retry", "/quit"))

    assert cli.repl(agent, {}) == 0
    assert len(histories) == 2
    assert [m for m in histories[0] if m.get("role") == "user"] == []
    assert [m for m in histories[1] if m.get("role") == "user"] == []
    assert [m["content"] for m in agent.messages if m.get("role") == "user"] == ["hello"]


def test_invalid_set_value_does_not_mutate_or_save_config(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    cfg = {"temperature": 0.4}
    monkeypatch.setattr(
        cli.config,
        "save",
        lambda _cfg: pytest.fail("invalid setting must not be saved"),
    )
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/set temperature hot", "/quit"))
    assert cli.repl(agent, cfg) == 0
    assert cfg == {"temperature": 0.4}


def test_repl_config_write_failure_returns_to_prompt(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli.config,
        "save",
        lambda _cfg: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/stream off", "/quit"))
    assert cli.repl(agent, {}) == 0


def test_gateway_config_write_failure_is_a_command_error(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(
        gateway_module._config,
        "save",
        lambda _cfg: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = gateway.handle_cmd("yolo", "on")
    assert result["ok"] is False
    assert "could not save config" in result["text"]


def test_session_write_failures_do_not_crash_command_surfaces(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(
        cli.sessions,
        "save",
        lambda _session: (_ for _ in ()).throw(OSError("disk full")),
    )
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/save", "/quit"))
    assert cli.repl(agent, {}) == 0

    gateway = _gateway(tmp_path, monkeypatch)
    monkeypatch.setattr(
        gateway_module.sessions,
        "save",
        lambda _session: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = gateway.handle_cmd("clear")
    assert result["ok"] is False
    assert "command failed" in result["text"]


def test_every_terminal_catalog_command_has_a_safe_smoke_path(tmp_path, monkeypatch):
    probes = {
        "/new": "/new",
        "/resume": "/resume",
        "/sessions": "/sessions",
        "/search": "/search",
        "/save": "/save",
        "/rename": "/rename",
        "/delete": "/delete",
        "/clear": "/clear",
        "/retry": "/retry",
        "/context": "/context",
        "/compact": "/compact",
        "/effort": "/effort",
        "/notes": "/notes",
        "/note": "/note",
        "/remind": "/remind",
        "/todo": "/todo",
        "/name": "/name",
        "/cd": "/cd",
        "/init": "/init",
        "/diff": "/diff",
        "/undo": "/undo",
        "/rewind": "/rewind",
        "/tools": "/tools",
        "/groups": "/groups",
        "/plan": "/plan off",
        "/accept": "/accept off",
        "/yolo": "/yolo off",
        "/rules": "/rules",
        "/mcp": "/mcp",
        "/browser": "/browser",
        "/gateway": "/gateway off",
        "/login": "/login",
        "/logout": "/logout",
        "/whoami": "/whoami",
        "/model": "/model",
        "/models": "/models",
        "/host": "/host",
        "/stream": "/stream on",
        "/config": "/config",
        "/set": "/set",
        "/diag": "/diag",
        "/help": "/help",
        "/quit": "/quit",
    }
    catalog = {name for _section, entries in COMMAND_GROUPS for name, _args, _hint in entries}
    assert set(probes) == catalog

    # /init asks the model to draft an AGENTS.md, which a stub client can't
    # answer. An existing file makes it take its refuse-and-explain branch,
    # which is the path this smoke test is here to check.
    (tmp_path / "AGENTS.md").write_text("# already here\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_settle_in", lambda _agent: None)
    monkeypatch.setattr(cli.config, "save", lambda _cfg: None)
    monkeypatch.setattr(cli.sessions, "save", lambda _session: None)
    monkeypatch.setattr(cli.sessions, "list_all", lambda: [])
    monkeypatch.setattr(cli.sessions, "search", lambda _query: [])
    monkeypatch.setattr(cli._notes, "list_all", lambda: [])
    monkeypatch.setattr(cli._reminders, "list_all", lambda **_kwargs: [])

    for name, probe in probes.items():
        agent = Agent(_CommandClient(), "test-model", tmp_path, stream=True)
        agent.state.update(mcp=_MCPStub(), browser=_BrowserStub())
        lines = (probe, "/quit")
        monkeypatch.setattr(cli, "Prompt", lambda lines=lines: _ScriptedPrompt(*lines))
        assert cli.repl(agent, {}, {"server": None}) == 0, name


def test_terminal_command_aliases_have_safe_smoke_paths(tmp_path, monkeypatch):
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_settle_in", lambda _agent: None)
    monkeypatch.setattr(cli._reminders, "list_all", lambda **_kwargs: [])
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/reminders", "/quit"))
    assert cli.repl(agent, {}) == 0


def test_exit_command_is_removed_and_quit_is_the_only_leave_command(tmp_path, monkeypatch, capsys):
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_settle_in", lambda _agent: None)
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/exit", "/quit"))
    assert cli.repl(agent, {}) == 0
    assert "unknown command: /exit" in capsys.readouterr().out
    catalog = {name for _section, entries in COMMAND_GROUPS for name, _args, _hint in entries}
    assert "/exit" not in catalog
    assert "/quit" in catalog


def test_commands_reject_ignored_extra_arguments(tmp_path, monkeypatch, capsys):
    agent = _agent(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "Prompt", lambda: _ScriptedPrompt("/models extra", "/quit"))
    assert cli.repl(agent, {}) == 0
    assert "usage: /models" in capsys.readouterr().out

    gateway = _gateway(tmp_path, monkeypatch)
    assert gateway.handle_cmd("clear", "extra") == {
        "ok": False,
        "text": "usage: /clear",
    }


def test_gateway_catalog_help_and_handlers_stay_in_sync(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path, monkeypatch, client=_CommandClient())
    gateway.agent.state.update(mcp=_MCPStub())
    monkeypatch.setattr(gateway_module._config, "save", lambda _cfg: None)
    monkeypatch.setattr(gateway_module.sessions, "save", lambda _session: None)
    monkeypatch.setattr(gateway, "new_chat", lambda: {"id": "new-chat"})
    probes = {
        "help": (),
        "new": (),
        "clear": (),
        "retry": (),
        "model": (),
        "models": (),
        "effort": (),
        "stream": ("on",),
        "tools": (),
        "groups": (),
        "plan": ("off",),
        "yolo": ("off",),
        "notes": (),
        "mcp": (),
        "name": (),
        "host": (),
        "config": (),
        "set": ("temperature", "0.5"),
        "diag": (),
    }
    assert set(probes) == GATEWAY_COMMAND_NAMES

    for command, args in probes.items():
        result = gateway.handle_cmd(command, *args)
        assert "unknown command" not in result["text"], command

    help_text = gateway.handle_cmd("help")["text"]
    assert gateway.handle_cmd("?")["text"] == help_text
    for name, _args, _hint in GATEWAY_COMMANDS:
        assert f"/{name}" in help_text
    assert "/save" not in help_text
    assert "/undo" not in help_text


def test_gateway_stream_name_and_set_apply_consistently(tmp_path, monkeypatch):
    cfg: dict = {}
    gateway = _gateway(tmp_path, monkeypatch, client=_CommandClient(), cfg=cfg)
    monkeypatch.setattr(gateway_module._config, "save", lambda _cfg: None)

    stream_result = gateway.handle_cmd("stream", "off")
    assert stream_result["ok"] is True
    assert gateway.engine.stream is False
    assert gateway.agent.engine.stream is False
    assert cfg["ollama"]["stream"] is False

    name_result = gateway.handle_cmd("name", "Ada", "Lovelace")
    assert name_result["ok"] is True
    assert gateway.agent.state.user_name == "Ada Lovelace"

    set_result = gateway.handle_cmd("set", "temperature", "1.25")
    assert set_result["ok"] is True
    assert gateway.engine.temperature == 1.25
    assert gateway.agent.engine.temperature == 1.25
    before = copy.deepcopy(cfg)
    invalid = gateway.handle_cmd("set", "gateway.port", "70000")
    assert invalid["ok"] is False
    assert cfg == before
    groups = gateway.handle_cmd("set", "tool_groups", "[]")
    assert groups["ok"] is True

    proactive = gateway.handle_cmd("set", "proactive.interval", "120")
    assert proactive["ok"] is True
    assert gateway.proactive_monitor.interval == 120
    assert "applied live" in proactive["text"]
    assert gateway.agent.state.tool_groups == set()


def test_gateway_retry_targets_the_last_visible_user_message(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path, monkeypatch)
    gateway.engine.messages.extend(
        [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "Tool result for read_file:\nsecret"},
            {"role": "user", "content": "second"},
            {"role": "assistant", "content": "reply two"},
        ]
    )
    before = copy.deepcopy(gateway.engine.messages)
    result = gateway.handle_cmd("retry")
    assert result == {
        "ok": True,
        "text": "retrying the most recent message",
        "action": {"type": "retry", "index": 1, "message": "second"},
    }
    assert gateway.engine.messages == before


def test_gateway_mcp_command_lists_one_servers_tools(tmp_path, monkeypatch):
    gateway = _gateway(tmp_path, monkeypatch)
    manager = SimpleNamespace(
        names=lambda: ["docs"],
        list_tools=lambda server: [{"name": "search", "description": f"Search {server} documents"}],
    )
    gateway.agent.state.update(mcp=manager)
    result = gateway.handle_cmd("mcp", "docs")
    assert result["ok"] is True
    assert "search" in result["text"]


def test_gateway_frontend_executes_retry_actions():
    source = (Path(gateway_module.__file__).parent / "gateway_assets" / "app.js").read_text()
    assert "d.action.type==='retry'" in source
    assert "streamEdit(idx,String(d.action.message||''))" in source


def test_gateway_frontend_reindexes_users_after_history_truncation():
    source = (
        Path(__file__).resolve().parents[1] / "cagentic" / "gateway_assets" / "app.js"
    ).read_text()
    assert "_userMsgIdx=thread.querySelectorAll('.msg-row.user').length" in source


def test_service_commands_report_missing_service_manager(tmp_path, monkeypatch, capsys):
    unit = tmp_path / "cagentic.service"
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "_systemd_unit_path", lambda: unit)
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("systemctl")),
    )
    assert service.install() == 1
    output = capsys.readouterr().out
    assert "partial state" in output
    assert unit.name in output
    assert unit.exists()


def test_service_uninstall_keeps_unit_when_disable_fails(tmp_path, monkeypatch, capsys):
    unit = tmp_path / "cagentic.service"
    unit.write_text("unit")
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "_systemd_unit_path", lambda: unit)
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr="not running", stdout=""),
    )
    assert service.uninstall() == 1
    assert unit.exists()
    output = " ".join(capsys.readouterr().out.split())
    assert "manager state may be partial" in output


def test_systemd_executable_paths_are_quoted():
    assert service._systemd_quote("/tmp/Python Tools/python") == '"/tmp/Python Tools/python"'
