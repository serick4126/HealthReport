from configparser import ConfigParser
from pathlib import Path


def load_config(config_path: str | None = None) -> ConfigParser:
    if config_path is None:
        config_path = Path(__file__).parent / "config.ini"
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {config_path}")
    config = ConfigParser()
    config.read(path, encoding="utf-8")
    return config
