import json
from unittest import mock

import pytest

from promptloom import glean


def test_backend_url():
    assert glean.backend_url("acme") == "https://acme-be.glean.com"
    assert glean.backend_url("acme-be.glean.com/") == "https://acme-be.glean.com"
    assert glean.backend_url("https://x.example.com") == "https://x.example.com"


def test_search_parses_results():
    reply = {"results": [{"title": "Runbook", "url": "https://g/1",
                          "snippets": [{"text": "restart the thing"}],
                          "document": {"datasource": "confluence"}}]}
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(reply).encode()
    cfg = {"glean": {"url": "acme"}, "keys": {"glean": "tok"}}
    with mock.patch("urllib.request.urlopen", return_value=response) as opened:
        hits = glean.search("runbook", 5, cfg)
    request = opened.call_args[0][0]
    assert request.full_url == "https://acme-be.glean.com/rest/api/v1/search"
    assert request.headers["Authorization"] == "Bearer tok"
    assert hits == [{"title": "Runbook", "url": "https://g/1", "source": "confluence",
                     "type": "", "snippet": "restart the thing"}]


def test_search_needs_setup(monkeypatch):
    monkeypatch.delenv("GLEAN_API_TOKEN", raising=False)
    monkeypatch.delenv("GLEAN_URL", raising=False)
    with pytest.raises(glean.GleanError):
        glean.search("x", 5, {"keys": {}})
