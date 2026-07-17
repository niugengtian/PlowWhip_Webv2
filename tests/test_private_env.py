from __future__ import annotations

import os
from pathlib import Path

import pytest

from plow_whip_web.config import load_private_env


def test_private_env_requires_private_permissions_and_never_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / ".env.local"
    path.write_text(
        "DEEPSEEK_API_KEY=local-secret\nDEEPSEEK_MODEL='local-model'\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.setenv("DEEPSEEK_MODEL", "process-model")
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    assert load_private_env(path) is True
    assert os.environ["DEEPSEEK_API_KEY"] == "local-secret"
    assert os.environ["DEEPSEEK_MODEL"] == "process-model"

    path.chmod(0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        load_private_env(path)


def test_private_env_missing_file_is_optional(tmp_path: Path) -> None:
    assert load_private_env(tmp_path / ".env.local") is False
