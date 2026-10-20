import pytest

from bambuddy_dragonbreath.config import load_config


def test_yaml_with_env_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("KEY", "bb_secret")
    f = tmp_path / "c.yaml"
    f.write_text(
        "bambuddy:\n  url: http://b:8000/\n  api_key: ${KEY}\n"
        "printers:\n  - printer_id: 1\n    dragonbreath_url: http://db/\n    dragonbreath_token: ${TOK:-x}\n"
    )
    cfg = load_config(f)
    assert cfg.bambuddy.url == "http://b:8000"
    assert cfg.bambuddy.api_key == "bb_secret"
    assert cfg.printers[0].dragonbreath_url == "http://db"
    assert cfg.printers[0].dragonbreath_token == "x"


def test_env_only(tmp_path, monkeypatch):
    for k, v in {
        "BAMBUDDY_URL": "https://bambuddy.example",
        "BAMBUDDY_API_KEY": "k",
        "PRINTER_ID": "1",
        "PRINTER_NAME": "Black Betty",
        "DRAGONBREATH_URL": "http://dragonbreath-bambu.lan",
        "OVERRIDES": "ABS=55, PETG=0",
        "OFF_DELAY_S": "30",
        "DRY_RUN": "true",
    }.items():
        monkeypatch.setenv(k, v)
    cfg = load_config(tmp_path / "missing.yaml")
    assert cfg.printers[0].printer_id == 1
    assert cfg.printers[0].label == "Black Betty"
    assert cfg.printers[0].dragonbreath_token == "bambuddy"
    assert cfg.overrides == {"ABS": 55, "PETG": 0}
    assert cfg.off_delay_s == 30
    assert cfg.dry_run is True


def test_env_only_missing_required(tmp_path, monkeypatch):
    monkeypatch.delenv("BAMBUDDY_URL", raising=False)
    with pytest.raises(SystemExit):
        load_config(tmp_path / "missing.yaml")
