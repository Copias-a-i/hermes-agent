"""Git trampoline recovery; branch updates use the real target-identity suite."""

import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


@pytest.fixture(autouse=True)
def _isolate_venv_holders(monkeypatch):
    """The update flow's venv-holder guard sees the live gateway processes on
    a dev machine and aborts with SystemExit 2 before reaching the branch
    logic under test.  Isolate it so the test exercises the intended path."""
    monkeypatch.setattr("hermes_cli.update_cmd_windows._detect_venv_python_processes", lambda: [])


class TestGitTrampolineSelfHeal:
    """Proactive Git-for-Windows trampoline self-heal (#87876).

    A broken bin\\git.exe / cmd\\git.exe shim (~46KB) refuses every git call
    with a "BUG (fork bomb)" guard instead of re-execing the real git-core
    binary. _ensure_non_trampoline_git detects this up front and swaps in a
    real git binary when one can be located, so the normal git update path
    survives instead of degrading to the ZIP fallback.
    """

    @staticmethod
    def _fake_run_healthy(command, **_kwargs):
        return subprocess.CompletedProcess(
            command, 0, stdout="git version 2.50.0.windows.1\n", stderr=""
        )

    @staticmethod
    def _fake_run_trampoline(command, **_kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="BUG (fork bomb): tried to spawn itself, check your PATH\n",
        )

    @pytest.mark.platforms("windows")
    def test_healthy_git_command_unchanged(self):
        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_healthy,
            ),
            patch("hermes_cli.update_cmd._locate_real_git") as locate,
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        locate.assert_not_called()

    @pytest.mark.platforms("windows")
    def test_trampoline_swaps_to_real_git(self, capsys):
        from pathlib import Path

        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        real = Path(r"C:\Program Files\Git\mingw64\libexec\git-core\git.exe")
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_trampoline,
            ),
            patch(
                "hermes_cli.update_cmd._locate_real_git", return_value=real
            ),
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == [str(real), "-c", "windows.appendAtomically=false"]
        out = capsys.readouterr().out
        assert "switching to real git" in out

    @pytest.mark.platforms("windows")
    def test_trampoline_no_real_git_keeps_command(self, capsys):
        from hermes_cli import update_cmd

        git_cmd = ["git", "-c", "windows.appendAtomically=false"]
        with (
            patch(
                "hermes_cli.update_cmd.subprocess.run",
                side_effect=self._fake_run_trampoline,
            ),
            patch("hermes_cli.update_cmd._locate_real_git", return_value=None),
        ):
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        out = capsys.readouterr().out
        assert "ZIP path" in out

    @pytest.mark.platforms("not windows")
    def test_off_windows_noop(self):
        from hermes_cli import update_cmd

        git_cmd = ["git"]
        with patch("hermes_cli.update_cmd.subprocess.run") as run:
            result = update_cmd._ensure_non_trampoline_git(git_cmd)
        assert result == git_cmd
        run.assert_not_called()

    def test_locate_real_git_uses_platform_resolver_not_git_core_layout(self, tmp_path, monkeypatch):
        """The managed runner Git may live outside Git-for-Windows' git-core tree."""
        from hermes_cli import update_cmd_git

        git = tmp_path / "runner-git.exe"
        git.touch()
        calls = []

        def resolve(name):
            calls.append(name)
            return SimpleNamespace(command=(str(git),))

        monkeypatch.setattr(update_cmd_git, "locate_command", resolve, raising=False)
        monkeypatch.setattr(update_cmd_git, "_probe_fork_bomb", lambda argv: False)

        assert update_cmd_git._locate_real_git() == git
        assert calls == ["git"]

    def test_portable_git_candidates_check_shared_root_first(self, tmp_path, monkeypatch):
        # Profile-scoped layout: HERMES_HOME = <root>/profiles/foo, but the
        # PortableGit tree lives under the SHARED root (monerostar review on
        # #88136). The candidate list must check get_default_hermes_root()
        # before the profile home.
        import hermes_constants
        from hermes_cli.update_cmd_git import _portable_git_candidates

        root = tmp_path / "root"
        profile_home = root / "profiles" / "foo"

        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: root)
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: profile_home)

        candidates = _portable_git_candidates()
        assert candidates[0] == (
            root / "git" / "mingw64" / "libexec" / "git-core" / "git.exe"
        )
        assert candidates[1] == (
            profile_home / "git" / "mingw64" / "libexec" / "git-core" / "git.exe"
        )

    @staticmethod
    def _stage_git_search(tmp_path, monkeypatch):
        """A PATH git (the shim under test) plus a managed PortableGit under the shared root, and no
        Program Files Git, so the search order is the same on every host."""
        import hermes_constants
        from hermes_cli import update_cmd_git

        shim = tmp_path / "bin" / "git.exe"
        real = tmp_path / "root" / "git" / "mingw64" / "libexec" / "git-core" / "git.exe"
        for exe in (shim, real):
            exe.parent.mkdir(parents=True)
            exe.touch()
        monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path / "root")
        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path / "root" / "profiles" / "foo")
        monkeypatch.setattr(update_cmd_git, "_GIT_FOR_WINDOWS_ROOTS", ())
        monkeypatch.setattr(
            update_cmd_git, "locate_command", lambda name: SimpleNamespace(command=(str(shim),)), raising=False
        )
        return shim, real

    def test_locate_real_git_recovers_when_the_path_git_is_the_broken_trampoline(self, tmp_path, monkeypatch):
        """#87876: the resolver's PATH hit is the very shim ``_ensure_non_trampoline_git`` just rejected, so
        the real ``git-core/git.exe`` (here the managed PortableGit) must still be found."""
        from hermes_cli import update_cmd_git

        shim, real = self._stage_git_search(tmp_path, monkeypatch)
        monkeypatch.setattr(update_cmd_git, "_probe_fork_bomb", lambda argv: argv[0] == str(shim))

        assert update_cmd_git._locate_real_git() == real

    def test_locate_real_git_failed_probe_disqualifies_like_a_guard_hit(self, tmp_path, monkeypatch):
        from hermes_cli import update_cmd_git

        shim, real = self._stage_git_search(tmp_path, monkeypatch)
        monkeypatch.setattr(
            update_cmd_git, "_probe_fork_bomb", lambda argv: None if argv[0] == str(shim) else False
        )
        assert update_cmd_git._locate_real_git() == real

        monkeypatch.setattr(update_cmd_git, "_probe_fork_bomb", lambda argv: None)
        assert update_cmd_git._locate_real_git() is None
