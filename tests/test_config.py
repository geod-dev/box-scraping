import pytest

from competitor_catalog.core.adapter import load_adapter
from competitor_catalog.core.config import ConfigError, load_competitor_configs, parse_competitor_config
from competitor_catalog.settings import DEFAULT_COMPETITOR_CONFIG_DIR

VALID = {
    "key": "acme",
    "name": "Acme",
    "base_url": "https://www.acme.test",
    "adapter": "tests.toy_adapter:ToyAdapter",
    "root_urls": ["https://www.acme.test/root"],
}


def test_repository_configs_load_and_adapters_resolve():
    configs = load_competitor_configs(DEFAULT_COMPETITOR_CONFIG_DIR)
    assert "antalis_fr" in configs
    antalis = configs["antalis_fr"]
    assert antalis.request_interval_seconds >= 4
    assert antalis.scrapedo["render"] is False
    adapter = load_adapter(antalis)
    assert type(adapter).__name__ == "AntalisAdapter"


def test_defaults():
    config = parse_competitor_config(dict(VALID))
    assert config.request_interval_seconds == 10.0
    assert config.host == "www.acme.test"


@pytest.mark.parametrize(
    "change",
    [
        {"adapter": "no_colon"},
        {"root_urls": ["https://elsewhere.test/x"]},
        {"key": ""},
        {"unexpected": 1},
    ],
)
def test_invalid_configs(change):
    with pytest.raises(ConfigError):
        parse_competitor_config({**VALID, **change})


def test_unknown_adapter_class():
    with pytest.raises(ConfigError):
        load_adapter(parse_competitor_config({**VALID, "adapter": "tests.toy_adapter:Nope"}))
