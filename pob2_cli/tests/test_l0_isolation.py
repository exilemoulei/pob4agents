"""Unit tests for pob2_cli L0 isolation, os_util hidden spawns, errors, locale_cfg, and CLI skeleton."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from pob2_cli import __version__, cli, compat, errors, game_profile, json_io, locale_cfg, os_util


class _FakeStartupInfo:
    dwFlags: int = 0
    wShowWindow: int = 0


def _ensure_win32_env(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        monkeypatch.setattr(sys, "platform", "win32")
        for attr, val in (
            ("CREATE_NEW_PROCESS_GROUP", 0x00000200),
            ("CREATE_NO_WINDOW", 0x08000000),
            ("STARTF_USESHOWWINDOW", 0x00000001),
            ("SW_HIDE", 0),
            ("STARTUPINFO", _FakeStartupInfo),
        ):
            monkeypatch.setattr(subprocess, attr, val, raising=False)


def test_game_profile_ignores_poe1_env_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB_RUNTIME_DIR", str(tmp_path / "poe1_rt"))
    monkeypatch.setenv("POB_HOME", str(tmp_path / "poe1_home"))
    monkeypatch.setenv("POB_LANG", "en")
    monkeypatch.setenv("POB_SLOT", "poe1slot")
    monkeypatch.setenv("POB_OFFLINE", "1")
    monkeypatch.delenv("POB2_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("POB2_HOME", raising=False)
    monkeypatch.delenv("POB2_LANG", raising=False)
    monkeypatch.delenv("POB2_SLOT", raising=False)
    monkeypatch.delenv("POB2_OFFLINE", raising=False)

    assert game_profile.GAME_ID == "poe2"
    assert game_profile.TOKEN_HEADER == "X-PoB2-Token"
    assert game_profile.default_slot() == "main"
    assert game_profile.is_offline() is False
    assert game_profile.resolve_runtime_dir() != tmp_path / "poe1_rt"
    assert locale_cfg.get_active_lang(runtime_dir=tmp_path / "fresh_rt") == ("zh-CN", "default")
    assert game_profile.stamp_game({"ok": True}) == {"ok": True, "game": "poe2"}


def test_game_profile_installed_app_dirs_windows_and_posix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(game_profile, "detect_checkout_pkg_root", lambda: None)
    monkeypatch.delenv("POB2_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("POB2_HOME", raising=False)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localapp"))
    assert game_profile.resolve_runtime_dir() == tmp_path / "localapp" / "pob24agents"

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert game_profile.resolve_runtime_dir() == tmp_path / "xdg" / "pob24agents"


def test_poe2_class_ids_match_non_contiguous_spec() -> None:
    assert game_profile.POE2_CLASS_IDS == (1, 2, 6, 7, 8, 9, 10, 11)
    assert set(game_profile.POE2_CLASS_NAMES.keys()) == set(game_profile.POE2_CLASS_IDS)
    assert game_profile.POE2_CLASS_NAMES[1] == "Witch"
    assert game_profile.POE2_CLASS_NAMES[10] == "Monk"
    assert game_profile.POE2_CLASS_NAMES[11] == "Druid"


def test_locale_cfg_fold_han_and_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POB2_LANG", raising=False)
    assert locale_cfg.fold_han("聖宗・法師之血・鍊結") == "圣宗・法师之血・链结"
    assert locale_cfg.lang_to_data_subdir("zh-CN") == "zh-rCN"
    assert locale_cfg.lang_to_data_subdir("zh-TW") == "zh-rTW"
    assert locale_cfg.lang_to_data_subdir("en") is None

    saved = locale_cfg.set_active_lang("zh-tw", runtime_dir=tmp_path)
    assert saved["lang"] == "zh-TW"
    assert locale_cfg.get_active_lang(runtime_dir=tmp_path) == ("zh-TW", "config")

    monkeypatch.setenv("POB2_LANG", "en")
    assert locale_cfg.get_active_lang(runtime_dir=tmp_path) == ("en", "env")


def test_os_util_hidden_windows_spawns_and_breakaway_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _ensure_win32_env(monkeypatch)
    kwargs = os_util.win_hidden_subprocess_kwargs(windows=True)
    flags = int(kwargs.get("creationflags", 0))
    assert flags & subprocess.CREATE_NO_WINDOW
    assert kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert kwargs["startupinfo"].wShowWindow == getattr(subprocess, "SW_HIDE", 0)

    calls: list[dict[str, Any]] = []

    def _fake_popen(_cmd: Any, **kw: Any) -> object:
        calls.append(kw)
        if int(kw.get("creationflags", 0)) & os_util._CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError("job denied")
        return object()

    monkeypatch.setattr(subprocess, "Popen", _fake_popen)
    os_util.spawn_detached_process(["python", "-V"], windows=True)
    assert len(calls) == 2
    assert calls[0]["creationflags"] & os_util._CREATE_BREAKAWAY_FROM_JOB
    assert not (calls[1]["creationflags"] & os_util._CREATE_BREAKAWAY_FROM_JOB)
    assert calls[1]["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_errors_exit_codes_and_json_roundtrip() -> None:
    assert errors.EXIT_OK == 0
    assert errors.EXIT_REQUEST_ERROR == 1
    assert errors.EXIT_DAEMON_ERROR == 5

    cross = errors.CrossGameHallucinationError(
        trigger="Mageblood",
        error="PoE1 item sent to pob2",
        remediation="Use pob for PoE1 builds",
        equivalent_command="pob data unique Mageblood",
        rule_id="R-POE1-UNIQUE",
    )
    d = cross.to_dict()
    assert d["game"] == "poe2"
    assert d["detectedGame"] == "poe1"
    assert d["errorKind"] == "cross_game_hallucination"
    assert errors.CrossGameHallucinationError.from_dict(d).to_dict() == d

    trap = errors.LegacyPatchTrapError(
        trigger="Recombinator",
        patch_version="0.5.0",
        error="Removed in 0.5.0",
        remediation="Use Omens/Essences/Desecration",
    )
    assert trap.to_dict()["game"] == "poe2"
    assert trap.to_dict()["errorKind"] == "legacy_patch_trap"


def test_json_io_and_compat_l0_helpers(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "state.json"
    json_io.atomic_write_json(target, {"name": "精魂", "ok": True})
    assert json_io.read_json_dict(target) == {"name": "精魂", "ok": True}
    assert json_io.read_json_dict(tmp_path / "missing.json") is None

    def _narrow(a: int, b: int = 2) -> int:
        return a + b

    assert compat.call_compat(_narrow, 3, b=4, extra="ignored") == 7
    assert os_util.is_pid_alive(os.getpid()) is True
    assert os_util.is_pid_alive(-1) is False


def test_cli_version_about_lang_and_dynamic_extension(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POB2_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("POB2_LANG", raising=False)
    runner = CliRunner()

    res_ver = runner.invoke(cli.main, ["--version"])
    assert res_ver.exit_code == 0
    assert f"pob2, version {__version__}" in res_ver.output

    res_about = runner.invoke(cli.main, ["about", "--json"])
    assert res_about.exit_code == 0
    payload = json.loads(res_about.output)
    assert payload["game"] == "poe2"
    assert payload["license"] == "GPL-3.0-only"
    assert payload["tokenHeader"] == "X-PoB2-Token"
    assert any("lifan-builds/poe2brain" in item for item in payload["attribution"])

    res_lang = runner.invoke(cli.main, ["lang", "zh-TW", "--json"])
    assert res_lang.exit_code == 0
    assert json.loads(res_lang.output)["lang"] == "zh-TW"

    @click.command("demo-ext")
    def _demo_ext() -> None:
        click.echo("ext-ok")

    cli.register_command(_demo_ext)
    try:
        res_ext = runner.invoke(cli.main, ["demo-ext"])
        assert res_ext.exit_code == 0
        assert "ext-ok" in res_ext.output
    finally:
        cli.main.commands.pop("demo-ext", None)
