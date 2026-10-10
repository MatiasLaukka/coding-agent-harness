from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from coding_agent_harness import cli
from coding_agent_harness.worker import QwenWorker


def test_run_command_collects_real_git_evidence(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "existing.txt").write_text("before\n", encoding="utf-8")

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-q")
    git("add", "existing.txt")
    git(
        "-c", "user.name=Harness Test",
        "-c", "user.email=harness@example.test",
        "-c", "commit.gpgsign=false",
        "commit", "-qm", "baseline",
    )
    baseline = git("rev-parse", "HEAD")

    worker_script = tmp_path / "offline_worker.py"
    worker_script.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "Path('existing.txt').write_text('after\\n', encoding='utf-8')\n"
        "Path('new.txt').write_text('new file\\n', encoding='utf-8')\n"
        "print('HARNESS_RESULT:' + json.dumps({\n"
        "    'status': 'completed',\n"
        "    'summary': 'Updated the tracked file and added a file.',\n"
        "    'changed_files': ['existing.txt', 'new.txt'],\n"
        "    'remaining_concerns': [],\n"
        "}))\n",
        encoding="utf-8",
    )

    def offline_worker(*, command, model, timeout_seconds):
        # Redirect only the external executable. Keep the real adapter,
        # orchestrator, Git evidence, and independent verifier.
        return QwenWorker(
            command=[sys.executable, str(worker_script)],
            model=model,
            timeout_seconds=timeout_seconds,
        )

    monkeypatch.setattr(cli, "QwenWorker", offline_worker)

    task_file = tmp_path / "task.json"
    task_file.write_text(
        json.dumps({
            "task": {
                "task_id": "real-git-integration",
                "goal": "Update the tracked file and add a file.",
                "acceptance": ["Both files contain the expected content."],
                "allowed_files": ["existing.txt", "new.txt"],
            },
            "verification_commands": [{
                "id": "check-content",
                "argv": [
                    sys.executable, "-c",
                    "from pathlib import Path; "
                    "assert Path('existing.txt').read_text() == 'after\\n'; "
                    "assert Path('new.txt').read_text() == 'new file\\n'",
                ],
            }],
        }),
        encoding="utf-8",
    )

    exit_code = cli.app([
        "run", str(task_file),
        "--repo", str(repo),
        "--model", "offline-test-model",
        "--worker-timeout", "30",
    ])
    output = capsys.readouterr()

    assert exit_code == 0, output.err
    assert output.err == ""
    assert "Status: ready-for-review" in output.out
    assert f"Baseline: {baseline}" in output.out
    assert f"Final HEAD: {baseline}" in output.out
    assert "Verification: PASS" in output.out
    assert "Findings:\n  (none)" in output.out
    assert "Changed files:\n  - existing.txt\n  - new.txt" in output.out
    assert "diff --git a/existing.txt b/existing.txt" in output.out
    assert "diff --git a/new.txt b/new.txt" in output.out
    assert "+after" in output.out
    assert "+new file" in output.out
    assert git("diff", "--cached", "--name-only") == ""
    assert git("rev-parse", "HEAD") == baseline
