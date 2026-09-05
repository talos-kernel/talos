"""First-run contracts: visible consent, atomic state, no implicit agent start."""
import io
import os
import stat

import pytest

from talos import cli, config, configcli, schema, setup_wizard as wizard
from talos.sandbox import MARKER
from tests.test_setup_wizard import FakeHttp, FakeStdin, CLAUDE_CLI, GOOD_TOKEN


class TerminalOutput(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.delenv(MARKER, raising=False)
    for key in (wizard.PRINCIPALS_KEY, 'TALOS_ALLOWED_USER_IDS', 'TALOS_CLAUDE_BIN'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, 'LOCAL_ENV', tmp_path / 'local.env')
    monkeypatch.setattr(config, 'SECRETS_ENV', tmp_path / 'secrets.env')
    return tmp_path / 'secrets.env'


def drive(path, answers, *, output=None, input_tty=True):
    output = output if output is not None else TerminalOutput()
    http = FakeHttp()
    code = wizard.run_setup(['terminal', '--out', str(path)],
                            stdin=FakeStdin(answers, tty=input_tty), stdout=output,
                            http=http, runtimes=CLAUDE_CLI)
    return code, output.getvalue(), http


def test_terminal_setup_persists_model_and_current_identity_without_telegram(isolated):
    code, text, http = drive(isolated, ['yes', '', ''])
    assert code == 0
    values = configcli.read_file(isolated)
    assert values[wizard.PRINCIPALS_KEY] == f'cli:{os.getuid()}'
    assert values[wizard.PROVIDER_KEY] == 'claude-cli'
    assert values['TALOS_CLAUDE_BIN'] == CLAUDE_CLI.claude
    assert wizard.TOKEN_KEY not in values
    assert stat.S_IMODE(isolated.stat().st_mode) == 0o600
    assert 'python -m talos chat' in text and 'not running' in text
    assert http.calls == []
    loaded = config.load_config(require_channel=False)
    assert loaded.claude_bin == CLAUDE_CLI.claude
    assert str(next(iter(loaded.allowed_principals))) == f'cli:{os.getuid()}'


def test_terminal_setup_keeps_existing_channels_and_unrelated_settings(isolated):
    config.LOCAL_ENV.write_text('TALOS_ALLOWED_PRINCIPALS=telegram:123\n')
    isolated.write_text('# keep this\nTALOS_STATUS_STYLE=expressive\n')
    assert drive(isolated, ['yes', '', ''])[0] == 0
    values = configcli.read_file(isolated)
    assert values[wizard.PRINCIPALS_KEY] == f'telegram:123,cli:{os.getuid()}'
    assert '# keep this' in isolated.read_text()
    assert values['TALOS_STATUS_STYLE'] == 'expressive'


@pytest.mark.parametrize('answers', [[], [''], ['no'], ['yes']])
def test_terminal_setup_cancel_or_eof_does_not_write(isolated, answers):
    original = 'TALOS_MODEL=keep-me\n'
    isolated.write_text(original)
    drive(isolated, answers)
    assert isolated.read_text() == original


@pytest.mark.parametrize('input_tty,output', [(False, TerminalOutput), (True, io.StringIO)])
def test_terminal_setup_requires_both_terminal_streams(isolated, input_tty, output):
    code, _, http = drive(isolated, ['yes', '', ''], input_tty=input_tty, output=output())
    assert code != 0 and not isolated.exists() and not http.calls


def test_terminal_setup_refuses_sandbox_and_conflicting_environment(isolated, monkeypatch):
    monkeypatch.setenv(MARKER, '1')
    assert drive(isolated, ['yes', '', ''])[0] != 0
    monkeypatch.delenv(MARKER)
    monkeypatch.setenv(wizard.PRINCIPALS_KEY, 'telegram:123')
    assert drive(isolated, ['yes', '', ''])[0] != 0
    assert not isolated.exists()


def test_setup_writes_all_keys_atomically_and_preserves_unrelated_state(tmp_path, monkeypatch):
    path = tmp_path / 'config.env'
    original = '# operator note\nTALOS_STATUS_STYLE=expressive\nTALOS_MODEL=old\n'
    path.write_text(original)
    values = ((wizard.MODEL_KEY, 'new'), (wizard.PROVIDER_KEY, 'claude-cli'))
    real_replace = os.replace
    def fail_replace(*args):
        assert path.read_text() == original
        raise OSError('disk unavailable')
    monkeypatch.setattr(os, 'replace', fail_replace)
    with pytest.raises(OSError):
        wizard._write_section(path, values)
    assert path.read_text() == original
    assert not path.with_name(path.name + '.neu').exists()
    monkeypatch.setattr(os, 'replace', real_replace)
    wizard._write_env(path, wizard.Bot(GOOD_TOKEN, 'Talos', 'example_bot'),
                      wizard.Principal('telegram', '123'), wizard.ModelSetup('claude-cli', 'new'))
    assert '# operator note' in path.read_text()
    assert configcli.read_file(path)['TALOS_STATUS_STYLE'] == 'expressive'
    assert configcli.read_file(path)['TALOS_MODEL'] == 'new'


def test_setup_refuses_symlink_and_multiline_values_without_touching_target(tmp_path):
    target = tmp_path / 'real.env'
    target.write_text('TALOS_MODEL=old\n')
    link = tmp_path / 'link.env'
    link.symlink_to(target)
    with pytest.raises(OSError):
        wizard._write_section(link, ((wizard.MODEL_KEY, 'new'),))
    with pytest.raises(ValueError):
        wizard._write_section(target, ((wizard.MODEL_KEY, 'new'), ('X', 'x\nY=z')))
    assert target.read_text() == 'TALOS_MODEL=old\n' and link.is_symlink()


@pytest.mark.parametrize('args', [['--oncie'], ['--once', '--bogus'], ['--version', '--once'],
                                 ['status', 'extra'], ['chat', 'extra']])
def test_bad_arguments_never_dispatch_an_agent_run(args):
    assert cli.dispatch(args) == 2


def test_command_help_and_typo_recovery_do_not_load_configuration(monkeypatch, capsys):
    monkeypatch.setattr(config, 'load_config', lambda **kw: pytest.fail('loaded config'))
    for command in ('config', 'setup', 'ask', 'chat', 'status', 'verify', 'version'):
        assert cli.dispatch(['help', command]) == 0
    assert cli.dispatch(['doctro']) == 2
    assert 'Did you mean talos doctor?' in capsys.readouterr().out


def test_config_search_and_describe_explain_without_disclosing_secrets(tmp_path):
    path = tmp_path / 'config.env'
    path.write_text('TELEGRAM_BOT_TOKEN=never-show-this\n')
    text = io.StringIO()
    assert configcli.run_config(['list', 'status', '--file', str(path)], out=text) == 0
    assert 'TALOS_STATUS_STYLE' in text.getvalue()
    assert 'TELEGRAM_BOT_TOKEN' not in text.getvalue()
    assert configcli.run_config(['describe', 'TELEGRAM_BOT_TOKEN', '--file', str(path)], out=text) == 0
    assert 'never-show-this' not in text.getvalue()
    assert 'secret' in text.getvalue() and 'talos setup' in text.getvalue()
    assert schema.get('TALOS_CLAUDE_BIN').kind == schema.POLICY
    assert configcli.run_config(['set', 'TALOS_STATUS_STYLE', 'expressive', '--file', str(path)], out=text) == 0
    assert configcli.run_config(['set', 'TALOS_STATUS_STYLE', 'expreessive', '--file', str(path)], out=text) != 0
    assert configcli.read_file(path)['TALOS_STATUS_STYLE'] == 'expressive'


def test_config_rejects_extra_arguments_and_reports_io_error(tmp_path):
    path = tmp_path / 'config.env'
    text = io.StringIO()
    assert configcli.run_config(['set', 'TALOS_MODEL', 'one', 'two', '--file', str(path)], out=text) != 0
    assert not path.exists()
    assert configcli.run_config(['set', 'TALOS_MODEL', 'one', '--file', str(path / 'missing')], out=text) != 0
    assert 'check the path and its permissions' in text.getvalue()


def test_terminal_color_is_optional_and_never_written_to_pipes(monkeypatch):
    from talos import terminalui

    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('TERM', 'xterm-256color')
    assert '\x1b[' in terminalui.paint('ready', out=TerminalOutput())
    assert terminalui.paint('ready', out=io.StringIO()) == 'ready'
    monkeypatch.setenv('NO_COLOR', '')
    assert terminalui.paint('ready', out=TerminalOutput()) == 'ready'
    monkeypatch.delenv('NO_COLOR')
    monkeypatch.setenv('TERM', 'dumb')
    assert terminalui.paint('ready', out=TerminalOutput()) == 'ready'


def test_terminal_labels_cannot_inject_control_sequences(monkeypatch):
    from talos import terminalui

    attack = '\x1b[2Jmodel\x1b]8;;https://example.invalid\x07link\x1b]8;;\x07\r\x07'
    result = terminalui.heading(attack, out=io.StringIO())
    assert '\x1b' not in result and '\r' not in result and '\x07' not in result
    assert 'modellink' in result


@pytest.mark.parametrize('with_environment', [True, False])
def test_launcher_uses_its_own_installation_from_another_directory(tmp_path, with_environment):
    import json
    import shutil
    import subprocess
    import sys
    from pathlib import Path

    root = tmp_path / 'installation with spaces'
    (root / 'bin').mkdir(parents=True)
    launcher = root / 'bin/talos'
    shutil.copy2(Path(__file__).resolve().parents[1] / 'bin/talos', launcher)
    if with_environment:
        (root / '.venv/bin').mkdir(parents=True)
        (root / '.venv/bin/python').symlink_to(sys.executable)
        (root / 'talos').mkdir()
        (root / 'talos/__init__.py').write_text('')
        (root / 'talos/__main__.py').write_text('import sys,json; print(json.dumps(sys.argv[1:]))')
    result = subprocess.run([str(launcher), 'ask', 'one argument with spaces'], cwd=tmp_path,
                            capture_output=True, text=True)
    if with_environment:
        assert result.returncode == 0 and json.loads(result.stdout) == ['ask', 'one argument with spaces']
    else:
        assert result.returncode == 2 and 'virtual environment is missing' in result.stderr
