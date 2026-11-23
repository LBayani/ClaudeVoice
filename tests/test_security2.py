"""Second audit round: /mcp from browsers, locked config fields, untrusted LAN text."""
from types import SimpleNamespace

import pytest

from voice_server import config, mcp_bridge
from voice_server.app import clean_field


def req(method="POST", remote="127.0.0.1", host="127.0.0.1:8765", **headers):
    headers.setdefault("Content-Type", "application/json")
    return SimpleNamespace(method=method, path="/mcp", remote=remote, host=host, headers=headers)


@pytest.mark.parametrize("r,why", [
    (req(), ""),                                                    # the claude CLI
    (req(**{"Sec-Fetch-Mode": "cors"}), ""),                        # node fetch adds this one
    (req(remote="192.168.1.7"), "remote"),
    (req(host="evil.example:8765"), "host"),
    (req(Origin="http://evil.example"), "browser"),
    (req(**{"Sec-Fetch-Site": "cross-site"}), "browser"),
    (req(**{"Content-Type": "text/plain"}), "content-type"),
])
def test_mcp_refuse_reason(r, why):
    assert mcp_bridge.mcp_refuse_reason(r) == why


def test_locked_keys():
    assert config.locked_keys({"tts": {"model_url": "http://x/evil.pt"}, "music": {"volume": 1}}) == ["tts.model_url"]
    assert set(config.locked_keys({"claude": {"executable": "x", "extra_args": ["--dangerously-skip-permissions"]},
                                   "models_dir": "C:/"})) == {"claude.executable", "claude.extra_args", "models_dir"}
    assert config.locked_keys({"openai": {"url": "http://127.0.0.1:1234/v1"}, "brain": "openai"}) == []


@pytest.mark.parametrize("raw", ['<img src=x onerror="alert(1)">', "2.4.0\"><script>", "a" * 500])
def test_clean_field(raw):
    v = clean_field(raw)
    assert "<" not in v and ">" not in v and '"' not in v and len(v) <= 40


def test_clean_field_keeps_real_values():
    assert clean_field("2.4.0") == "2.4.0"
    assert clean_field("e4:b0:63:aa:bb:cc") == "e4:b0:63:aa:bb:cc"
    assert clean_field("zhengchen-1.54tft-wifi") == "zhengchen-1.54tft-wifi"
