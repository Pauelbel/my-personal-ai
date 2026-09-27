"""Git-инструменты — граница безопасности: только чтение коммитов, только внутри рабочей папки."""

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from local_agent.tools.git import GitLogTool, GitShowTool


def git(repo: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments], cwd=repo, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout.strip()


def run(tool, arguments: dict, workspace: Path):
    return asyncio.run(tool.execute(arguments, workspace))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "app").mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Tester")
    git(root, "config", "user.email", "tester@example.com")
    git(root, "config", "commit.gpgsign", "false")
    (root / "app" / "main.py").write_text("print('v1')\n", encoding="utf-8")
    (root / "secret.txt").write_text("пароль\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "Первый коммит")
    (root / "app" / "main.py").write_text("print('v2')\n", encoding="utf-8")
    (root / "secret.txt").write_text("новый пароль\n", encoding="utf-8")
    git(root, "commit", "-q", "-am", "Второй коммит")
    return root


def state(repo: Path) -> tuple[str, str, str]:
    return git(repo, "rev-parse", "HEAD"), git(repo, "status", "--porcelain"), git(repo, "for-each-ref")


def test_log_lists_commits_with_limit(repo: Path) -> None:
    result = run(GitLogTool(), {"limit": 1}, repo)

    assert not result.is_error
    assert "Второй коммит" in result.content
    assert "Первый коммит" not in result.content


def test_show_returns_commit_diff(repo: Path) -> None:
    head = git(repo, "rev-parse", "--short", "HEAD")

    result = run(GitShowTool(), {"commit": head}, repo)

    assert not result.is_error
    assert "Второй коммит" in result.content
    assert "+print('v2')" in result.content


@pytest.mark.parametrize("commit", ["--output=hacked.txt", "HEAD", "HEAD~1", "main", "abc", "a" * 41, 123, None])
def test_show_accepts_only_commit_hash(repo: Path, commit) -> None:
    result = run(GitShowTool(), {"commit": commit}, repo)

    assert result.is_error
    assert not (repo / "hacked.txt").exists()


@pytest.mark.parametrize("limit", [0, 51, "5", True, 2.5])
def test_log_rejects_bad_limit(repo: Path, limit) -> None:
    assert run(GitLogTool(), {"limit": limit}, repo).is_error


@pytest.mark.parametrize("path", ["../secret.txt", "..", "/etc/passwd", "C:/Windows"])
def test_path_outside_workspace_is_rejected(repo: Path, path: str) -> None:
    workspace = repo / "app"
    head = git(repo, "rev-parse", "HEAD")

    assert run(GitLogTool(), {"path": path}, workspace).is_error
    assert run(GitShowTool(), {"commit": head, "path": path}, workspace).is_error


def test_workspace_inside_repo_does_not_see_files_outside(repo: Path) -> None:
    workspace = repo / "app"
    head = git(repo, "rev-parse", "HEAD")

    shown = run(GitShowTool(), {"commit": head}, workspace)
    # «:/» в обычном git означает корень репозитория; здесь это буквальное имя файла.
    magic = run(GitShowTool(), {"commit": head, "path": ":/secret.txt"}, workspace)

    assert "print('v2')" in shown.content
    assert "пароль" not in shown.content and "secret.txt" not in shown.content
    assert "пароль" not in magic.content


def test_not_a_repository(tmp_path: Path) -> None:
    result = run(GitLogTool(), {}, tmp_path)

    assert result.is_error
    assert "не является git-репозиторием" in result.content


def test_repository_config_cannot_run_programs(repo: Path) -> None:
    marker = repo.parent / "executed.txt"
    script = repo.parent / "evil.py"
    script.write_text(f"open({str(marker)!r}, 'w').write('ran')\n", encoding="utf-8")
    command = f'"{Path(sys.executable).as_posix()}" "{script.as_posix()}"'
    git(repo, "config", "diff.external", command)
    git(repo, "config", "diff.evil.textconv", command)
    (repo / ".gitattributes").write_text("*.py diff=evil\n", encoding="utf-8")
    head = git(repo, "rev-parse", "HEAD")

    # Контроль: обычный git show действительно запускает программу из конфигурации репозитория.
    subprocess.run(["git", "show", head], cwd=repo, capture_output=True)
    assert marker.exists()
    marker.unlink()

    run(GitShowTool(), {"commit": head}, repo)
    run(GitLogTool(), {}, repo)

    assert not marker.exists()


def test_tools_do_not_change_repository(repo: Path) -> None:
    before = state(repo)
    head = git(repo, "rev-parse", "HEAD")

    run(GitLogTool(), {"limit": 50}, repo)
    run(GitShowTool(), {"commit": head}, repo)
    run(GitShowTool(), {"commit": head, "path": "app"}, repo)

    assert state(repo) == before


def test_git_tools_do_not_need_approval() -> None:
    assert GitLogTool.requires_approval is False
    assert GitShowTool.requires_approval is False
