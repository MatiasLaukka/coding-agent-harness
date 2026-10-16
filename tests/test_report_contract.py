import pytest
from coding_agent_harness import cli

@pytest.mark.parametrize('option', ['--report', '--report=old.md', '--rep', '--report-d', '--report-di', '--report-', '--re'])
def test_old_spelling_and_prefixes_are_usage_errors(option, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'QwenWorker', lambda **kw: pytest.fail('worker setup'))
    argv = ['run', 'task.json', '--repo', '.', '--model', 'offline', option]
    if '=' not in option:
        argv.append('old.md')
    with pytest.raises(SystemExit) as error:
        cli.app(argv)
    assert error.value.code == 2
    assert 'unrecognized arguments: ' + option in capsys.readouterr().err

def test_new_directory_option_is_explicit():
    parsed = cli._build_parser().parse_args(['run', 'task.json', '--repo', '.', '--model', 'offline', '--report-dir', 'reports'])
    assert str(parsed.report_dir) == 'reports'
