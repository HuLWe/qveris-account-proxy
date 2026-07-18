from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
REGISTRATION_URL = "https://qveris.ai/?ref=afAfj_c90cnWYg"
INVITE_CODE = "75gxF1vtvXWj_A"


def read_project_file(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8-sig")


def find_bash() -> str:
    candidates: list[str | None] = []
    if os.name == "nt":
        program_files = Path(os.environ.get("ProgramFiles", "C:/Program Files"))
        candidates.extend(
            [
                str(program_files / "Git/bin/bash.exe"),
                str(program_files / "Git/usr/bin/bash.exe"),
            ]
        )
    candidates.append(shutil.which("bash"))

    windows_subsystem_launcher = (
        Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/bash.exe"
    )
    for candidate in dict.fromkeys(item for item in candidates if item):
        if not Path(candidate).is_file():
            continue
        if os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(
            os.path.abspath(windows_subsystem_launcher)
        ):
            continue
        try:
            version = subprocess.run(
                [candidate, "--version"],
                capture_output=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if version.returncode == 0:
            return candidate
    pytest.skip("Bash is not installed")


def write_shell_fixture(path: Path, content: str) -> None:
    path.write_bytes(textwrap.dedent(content).lstrip().encode("utf-8"))
    path.chmod(0o755)


def run_shell_quickstart_fixture(
    tmp_path: Path,
    arguments: list[str],
    *,
    bootstrap_available: bool = True,
    environment_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    bash = find_bash()
    project = tmp_path / "qveris-quickstart"
    project.mkdir()
    for name in (
        "start.sh",
        "compose.yaml",
        "compose.lite.yaml",
        "compose.ui.yaml",
        "compose.quickstart.yaml",
    ):
        shutil.copy2(ROOT / name, project / name)
    (project / "start.sh").chmod(0o755)

    fake_bin = project / "fake-bin"
    fake_bin.mkdir()
    docker_log = project / "docker-calls.log"
    write_shell_fixture(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        set -eu

        {
          printf 'BIND=%s\tROUTING=%s' "${QVP_BIND_ADDRESS-}" "${QVP_ROUTING_MODE-}"
          for argument in "$@"; do
            argument="${argument//$'\n'/<NL>}"
            argument="${argument//$'\t'/<TAB>}"
            printf '\t%s' "$argument"
          done
          printf '\n'
        } >>"$MOCK_DOCKER_LOG"

        if [[ "${1-}" == inspect ]]; then
          printf 'healthy\n'
          exit 0
        fi

        if [[ "${1-}" == exec ]]; then
          if [[ "${MOCK_BOOTSTRAP_FAIL:-0}" == 1 ]]; then
            exit 1
          fi
          for ((index = 0; index < 43; index++)); do
            printf 'b'
          done
          printf '\n'
          exit 0
        fi

        if [[ "${1-}" == volume && "${2-}" == inspect ]]; then
          volume_name="${@: -1}"
          volume_key="${volume_name#qvp-fixture_}"
          printf 'qvp-fixture|%s|1\n' "$volume_key"
          exit 0
        fi

        if [[ "${1-}" == compose ]]; then
          for argument in "$@"; do
            if [[ "$argument" == ps ]]; then
              printf 'fixture-container-id\n'
              break
            fi
          done
          exit 0
        fi

        if [[ "${1-}" == run ]]; then
          entrypoint=''
          for ((index = 1; index <= $#; index++)); do
            if [[ "${!index}" == --entrypoint ]]; then
              next=$((index + 1))
              entrypoint="${!next}"
              break
            fi
          done
          case "$entrypoint" in
            python)
              printf 'accounts-present\n'
              ;;
            cat)
              for ((index = 0; index < 64; index++)); do
                printf 'a'
              done
              printf '\n'
              ;;
          esac
        fi
        """,
    )
    write_shell_fixture(
        fake_bin / "xdg-open",
        """
        #!/usr/bin/env bash
        printf '%s\n' "${1-}" >"$MOCK_OPEN_LOG"
        exit 0
        """,
    )
    environment = os.environ.copy()
    environment["PATH"] = str(fake_bin) + os.pathsep + environment.get("PATH", "")
    environment["MOCK_DOCKER_LOG"] = str(docker_log)
    environment["MOCK_OPEN_LOG"] = str(project / "open-url.log")
    environment["MOCK_BOOTSTRAP_FAIL"] = "0" if bootstrap_available else "1"
    environment["QVP_PROJECT_NAME"] = "qvp-fixture"
    environment.pop("QVP_BIND_ADDRESS", None)
    environment.pop("QVP_HOST_PORT", None)
    environment.pop("QVP_LAN_HOST", None)
    environment.pop("QVP_ROUTING_MODE", None)
    if "--lan" in arguments:
        environment["QVP_LAN_HOST"] = "192.0.2.10"
    if environment_overrides:
        environment.update(environment_overrides)
    completed = subprocess.run(
        [bash, str(project / "start.sh"), *arguments],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    return completed, docker_log.read_text(encoding="utf-8"), project


def test_launchers_include_the_same_first_run_contract() -> None:
    powershell = read_project_file("start.ps1")
    shell = read_project_file("start.sh")

    for launcher in (powershell, shell):
        assert REGISTRATION_URL in launcher
        assert INVITE_CODE in launcher
        assert "compose.yaml" in launcher
        assert "compose.lite.yaml" in launcher
        assert "compose.ui.yaml" in launcher
        assert "compose.quickstart.yaml" in launcher
        assert "accounts.json" in launcher
        assert "proxy_access_token" in launcher
        assert "#access_token=" not in launcher
        assert "?access_token=" not in launcher
        assert "QVP_CONFIG_DIR" in launcher
        assert "QVP_ALLOW_API_KEY_FOR_OAUTH_ROUTES" in launcher
        assert "10001:10001" in launcher
        assert "qveris_config" in launcher
        assert "qveris_secrets" in launcher
        assert "io.github.hulwe.qveris.quickstart" in launcher
        assert "secrets.token_hex(32)" in launcher
        assert "bootstrap_ticket" in launcher
        assert "admin/v1/bootstrap-ticket" in launcher
        assert "?launch=" in launcher
        assert "0o700" in launcher
        assert "0o600" in launcher

    assert "-AsSecureString" in powershell
    assert "[switch]$Lan" in powershell
    assert "[switch]$Stop" in powershell
    assert "Invoke-DockerWithInput" in powershell
    assert '[guid]::NewGuid().ToString("N")' in powershell
    assert "Set-Clipboard" not in powershell
    assert '& docker exec --user "10001:10001"' in powershell
    assert "$_.Exception.Message" in powershell
    assert "QVP_ROUTING_MODE = $routingMode" in powershell
    assert "Resolve-LanHost -Override $env:QVP_LAN_HOST" in powershell
    assert "& docker @containerArguments" in powershell
    assert "& docker @($composeArguments" not in powershell
    assert "read -r -s api_key" in shell
    assert "--lan" in shell
    assert "--stop" in shell
    assert "printf '%s' \"$api_key\" | docker run" in shell
    assert "copy_to_clipboard" not in shell
    assert "docker exec --user 10001:10001" in shell
    assert 'QVP_ROUTING_MODE="${QVP_ROUTING_MODE:-round_robin}"' in shell
    assert 'api_host="$(resolve_lan_host)"' in shell
    assert "$RuntimeDir" not in powershell
    assert "RUNTIME_DIR=" not in shell


def test_windows_launcher_keeps_windows_powershell_compatible_encoding() -> None:
    assert (ROOT / "start.ps1").read_bytes().startswith(b"\xef\xbb\xbf")


def test_start_cmd_delegates_all_arguments_and_exit_status() -> None:
    command = read_project_file("start.cmd")
    lowered = command.lower()

    assert lowered.startswith("@echo off\nsetlocal\n")
    assert "powershell.exe" in lowered
    assert "-nologo" in lowered
    assert "-noprofile" in lowered
    assert "-executionpolicy bypass" in lowered
    assert '-file "%~dp0start.ps1" %*' in lowered
    assert 'set "qvp_exit=%errorlevel%"' in lowered
    assert 'if not "%qvp_exit%"=="0"' in lowered
    assert "pause" in lowered
    assert "exit /b %qvp_exit%" in lowered
    assert "qvp_bind_address" not in lowered
    assert "qvp_host_port" not in lowered


def test_launchers_default_to_loopback_and_require_an_explicit_lan_flag() -> None:
    powershell = read_project_file("start.ps1")
    shell = read_project_file("start.sh")

    assert "[switch]$Lan" in powershell
    assert "$bindInput = if ($Lan)" in powershell
    assert '"0.0.0.0"' in powershell
    assert '"127.0.0.1"' in powershell
    assert '$browserHost = "127.0.0.1"' in powershell

    assert "--lan)" in shell
    assert "bind_input='0.0.0.0'" in shell
    assert 'bind_input="${QVP_BIND_ADDRESS:-127.0.0.1}"' in shell
    assert "browser_host='127.0.0.1'" in shell
    assert "默认仅本机访问；--lan 监听所有 IPv4 网卡" in shell


def test_launchers_preserve_existing_configuration() -> None:
    powershell = read_project_file("start.ps1")
    shell = read_project_file("start.sh")

    for launcher in (powershell, shell):
        assert "if os.path.lexists(path):" in launcher
        assert "raise SystemExit(0)" in launcher
        assert "except FileExistsError:" in launcher
        assert "accounts-present" in launcher


def test_quickstart_overlay_replaces_host_secret_mounts() -> None:
    overlay = read_project_file("compose.quickstart.yaml")

    expected_mounts = {
        "qveris_config": "/config",
        "qveris_secrets": "/run/secrets",
        "qveris_account_secrets": "/run/account-secrets",
    }
    assert overlay.count("- type: volume") == len(expected_mounts)
    for source, target in expected_mounts.items():
        assert f"source: {source}" in overlay
        assert f"target: {target}" in overlay
        assert overlay.count(f"{source}:") == 1
    assert "target: /run/secrets\n        read_only: true" in overlay
    assert "target: /run/account-secrets\n        read_only: true" in overlay
    assert "type: bind" not in overlay
    assert "${" not in overlay
    assert overlay.count('io.github.hulwe.qveris.quickstart: "1"') == 3


def test_launchers_send_secrets_to_named_volumes_over_stdin() -> None:
    powershell = read_project_file("start.ps1")
    shell = read_project_file("start.sh")

    for launcher in (powershell, shell):
        assert "type=volume" in launcher
        assert "type=bind" not in launcher
        assert "QVP_API_KEY" not in launcher
        assert "QVP_ACCESS_TOKEN" not in launcher

    assert "$InputValue | & docker @Arguments" in powershell
    assert (
        "Invoke-DockerWithInput -Arguments $accountArguments -InputValue $ApiKey"
        in powershell
    )
    assert "WriteAllText" not in powershell
    assert "Set-Content" not in powershell
    assert "Out-File" not in powershell
    assert "New-Item" not in powershell

    assert "printf '%s' \"$api_key\" | docker run" in shell
    assert "export api_key" not in shell
    assert "export proxy_token" not in shell
    assert "RUNTIME_DIR=" not in shell
    assert "ACCOUNTS_FILE=" not in shell
    assert "TOKEN_FILE=" not in shell
    assert "mktemp" not in shell


@pytest.mark.parametrize(
    ("arguments", "expected_bind"),
    [
        ([], "127.0.0.1"),
        (["--lan"], "0.0.0.0"),
    ],
)
def test_shell_quickstart_uses_named_volumes_without_host_secret_files(
    tmp_path: Path, arguments: list[str], expected_bind: str
) -> None:
    completed, docker_calls, project = run_shell_quickstart_fixture(tmp_path, arguments)
    proxy_token = "a" * 64

    assert completed.returncode == 0, completed.stderr
    assert proxy_token not in completed.stdout
    assert proxy_token not in completed.stderr
    assert "管理页已自动连接" in completed.stdout
    assert "http://127.0.0.1:18081/admin/" in completed.stdout
    expected_api_host = "192.0.2.10" if "--lan" in arguments else "127.0.0.1"
    assert f"http://{expected_api_host}:18081/api/v1" in completed.stdout
    opened_url = (project / "open-url.log").read_text(encoding="utf-8").strip()
    assert re.fullmatch(
        r"http://127\.0\.0\.1:18081/admin/\?launch=\d+-\d+#bootstrap_ticket=b{43}",
        opened_url,
    )
    assert f"BIND={expected_bind}" in docker_calls
    assert "ROUTING=round_robin" in docker_calls
    assert docker_calls.count("\tvolume\tcreate") == 3
    assert "qvp-fixture_qveris_config" in docker_calls
    assert "qvp-fixture_qveris_secrets" in docker_calls
    assert "qvp-fixture_qveris_account_secrets" in docker_calls
    assert "compose.quickstart.yaml" in docker_calls
    assert "type=volume,source=" in docker_calls
    assert "type=bind" not in docker_calls
    assert docker_calls.count("\tvolume\tinspect") == 3

    assert not (project / "runtime").exists()
    forbidden_names = {"accounts.json", "proxy_access_token", ".env"}
    assert not any(path.name in forbidden_names for path in project.rglob("*"))
    encoded_token = proxy_token.encode("ascii")
    for path in project.rglob("*"):
        if path.is_file():
            assert encoded_token not in path.read_bytes()


def test_shell_quickstart_preserves_explicit_routing_mode(tmp_path: Path) -> None:
    completed, docker_calls, _ = run_shell_quickstart_fixture(
        tmp_path,
        [],
        environment_overrides={"QVP_ROUTING_MODE": "explicit"},
    )

    assert completed.returncode == 0, completed.stderr
    assert "ROUTING=explicit" in docker_calls


def test_shell_quickstart_reports_invalid_project_reason(tmp_path: Path) -> None:
    completed, _, _ = run_shell_quickstart_fixture(
        tmp_path,
        [],
        environment_overrides={"QVP_PROJECT_NAME": "INVALID PROJECT"},
    )

    assert completed.returncode == 1
    assert "QVP_PROJECT_NAME 格式无效" in completed.stderr


def test_shell_quickstart_prints_a_secret_safe_retrieval_command_when_bootstrap_fails(
    tmp_path: Path,
) -> None:
    completed, _, _ = run_shell_quickstart_fixture(
        tmp_path, [], bootstrap_available=False
    )
    proxy_token = "a" * 64

    assert completed.returncode == 0, completed.stderr
    assert proxy_token not in completed.stdout
    assert proxy_token not in completed.stderr
    assert "自动连接链接生成失败" in completed.stdout
    assert "qvp-fixture_qveris_secrets" in completed.stdout
    assert "/run/secrets/proxy_access_token" in completed.stdout


def test_shell_quickstart_stop_skips_build_and_preserves_volumes(
    tmp_path: Path,
) -> None:
    completed, docker_calls, _ = run_shell_quickstart_fixture(tmp_path, ["--stop"])

    assert completed.returncode == 0, completed.stderr
    assert "已停止" in completed.stdout
    assert "\tdown\t--remove-orphans" in docker_calls
    assert "\tbuild\tproxy" not in docker_calls
    assert "\tvolume\tcreate" not in docker_calls


def test_documentation_and_ignore_rules_match_the_delivery_flow() -> None:
    readme = read_project_file("README.md")
    gitignore = read_project_file(".gitignore")

    assert "## 3 步快速开始" in readme
    assert REGISTRATION_URL in readme
    assert INVITE_CODE in readme
    assert ".\\start.ps1" in readme
    assert "./start.sh" in readme
    assert ".\\start.cmd -Stop" in readme
    assert "./start.sh --stop" in readme
    assert "显示、隐藏或复制代理 API Key" in readme
    assert "sessionStorage" in readme

    ignored = set(gitignore.splitlines())
    assert "runtime/" in ignored
    assert ".env" in ignored
    assert ".env.*" in ignored
    assert "accounts.json" in ignored
    assert "proxy_access_token" in ignored
    assert "**/secrets/" in ignored
