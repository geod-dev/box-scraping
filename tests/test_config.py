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
    assert type(load_adapter(configs["cenpac_fr"])).__name__ == "CenpacAdapter"
    assert configs["cenpac_fr"].options["block_page_markers"] == ["<title>Request Rejected</title>"]
    excluded = configs["cenpac_fr"].options["excluded_top_level_category_ids"]
    assert len(excluded) == 12 and all(isinstance(x, str) for x in excluded)


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


def test_request_jitter_defaults_to_zero_and_is_read_when_set():
    assert parse_competitor_config(dict(VALID)).request_jitter_seconds == 0.0
    config = parse_competitor_config({**VALID, "request_jitter_seconds": 1})
    assert config.request_jitter_seconds == 1.0
    assert config.as_snapshot()["request_jitter_seconds"] == 1.0


def test_cenpac_rate_is_3s_with_1s_jitter():
    cenpac = load_competitor_configs(DEFAULT_COMPETITOR_CONFIG_DIR)["cenpac_fr"]
    assert cenpac.request_interval_seconds == 3.0
    assert cenpac.request_jitter_seconds == 1.0


@pytest.mark.parametrize("value", [-0.5, "1", None, True, float("nan"), float("inf")])
def test_invalid_request_jitter(value):
    with pytest.raises(ConfigError):
        parse_competitor_config({**VALID, "request_jitter_seconds": value})
