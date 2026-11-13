"""Unit tests for configuration precedence, secret separation, regional weights, and pathlib compliance."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from argenprecios.config import (
    load_config,
    load_government_periods,
    load_regional_weights,
    resolve_user_path,
)
from argenprecios.logging_setup import CredentialRedactingFilter


def test_config_precedence_and_env_secret_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yaml_workdir = (tmp_path / "yaml_wd").resolve()
    cli_workdir = (tmp_path / "cli_wd").resolve()
    cfg_file = tmp_path / "settings.yaml"
    cfg_file.write_text(
        yaml.safe_dump(
            {
                "workdir": str(yaml_workdir),
                "spans": {"max_bridge_gap_days": 2, "storage_budget_gb": 500},
                "web": {"host": "127.0.0.1", "port": 8080},
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv(
        "ARGENPRECIOS_DATABASE_URL",
        "postgresql+psycopg://argenprecios:super_secret_pw@127.0.0.1:5433/argenprecios",
    )
    monkeypatch.setenv("ARGENPRECIOS_SECRET_KEY", "env-signing-secret-key-999")

    # CLI workdir overrides YAML workdir
    cfg = load_config(config_path=cfg_file, workdir=cli_workdir)
    assert cfg.workdir == cli_workdir
    assert cfg.database_url.endswith(":5433/argenprecios")
    assert cfg.secret_key == "env-signing-secret-key-999"
    assert cfg.spans.max_bridge_gap_days == 2
    assert cfg.web.port == 8080

    # Ensure owned directories creates ONLY owned paths, never external producer paths
    cfg.ensure_owned_directories()
    assert cfg.logs_dir.is_dir()
    assert cfg.reports_dir.is_dir()
    assert cfg.sources.temp_extract_dir.is_dir()


def test_regional_weights_and_government_periods_yaml() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    weights = load_regional_weights(repo_root / "config" / "regional_weights.yaml")
    assert len(weights.regions) == 6

    prov_map = weights.province_to_region_map()
    assert len(prov_map) == 24
    assert prov_map["AR-U"] == "PATAGONIA"
    assert prov_map["AR-C"] == "GBA"

    nat_weights = weights.national_province_expenditure_weights()
    assert len(nat_weights) == 24
    assert sum(nat_weights.values()) == pytest.approx(1.0, abs=1e-3)

    periods_cfg = load_government_periods(repo_root / "config" / "government_periods.yaml")
    assert len(periods_cfg.periods) >= 2


def test_credential_redacting_log_filter() -> None:
    filt = CredentialRedactingFilter()
    rec = logging.LogRecord(
        name="argenprecios.test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Connecting to postgresql+psycopg://argenprecios:MyTopSecret123@127.0.0.1:5433/db",
        args=(),
        exc_info=None,
    )
    assert filt.filter(rec) is True
    assert "MyTopSecret123" not in rec.msg
    assert "***REDACTED***" in rec.msg


def test_zero_os_path_or_print_in_src() -> None:
    src_root = resolve_user_path(Path(__file__).resolve().parents[2] / "src" / "argenprecios")
    py_files = list(src_root.rglob("*.py"))
    assert py_files
    for py_file in py_files:
        text = py_file.read_text(encoding="utf-8")
        assert "os.path" not in text, f"Forbidden os.path in {py_file}"
        assert "os.makedirs" not in text, f"Forbidden os.makedirs in {py_file}"
        assert "os.mkdir" not in text, f"Forbidden os.mkdir in {py_file}"
        assert "session.query(" not in text, f"Forbidden legacy session.query in {py_file}"
        assert "metadata.create_all" not in text, f"Forbidden metadata.create_all in {py_file}"
