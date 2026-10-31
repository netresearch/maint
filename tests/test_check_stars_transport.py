"""Exercise transport reuse without making HTTP requests or sending messages."""

import requests
import pytest

from test_check_stars import check_stars


@pytest.fixture
def transport(monkeypatch):
    calls = []

    def send(session, request, **kwargs):
        calls.append((session, request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        if request.url.startswith("https://api.github.com/"):
            response._content = b"[]"
            response.headers["Content-Type"] = "application/json"
        else:
            response._content = b'<html><div id="dependents"></div></html>'
            response.headers["Content-Type"] = "text/html"
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    monkeypatch.setattr(check_stars, "DEPENDENTS_PAGE_INTERVAL", 0)
    monkeypatch.setattr(check_stars, "GITHUB_TOKEN", "synthetic-test-token")
    return calls


def test_api_requests_share_a_transport_session(transport):
    assert check_stars.github_request("https://api.github.com/repos/example/one") == []
    assert check_stars.github_request("https://api.github.com/repos/example/two") == []
    assert transport[0][0] is transport[1][0]
    assert all(call[2]["timeout"] == 30 for call in transport)


def test_html_requests_share_a_transport_session(transport):
    assert check_stars.get_dependents("example/one") == []
    assert check_stars.get_dependents("example/two") == []
    assert transport[0][0] is transport[1][0]
    assert all(call[2]["timeout"] == 30 for call in transport)


def test_html_transport_does_not_receive_api_authorization(transport):
    check_stars.github_request("https://api.github.com/repos/example/one")
    check_stars.get_dependents("example/one")
    api, page = transport
    assert api[0] is not page[0]
    assert api[1].headers["Authorization"] == "Bearer synthetic-test-token"
    assert "Authorization" not in page[1].headers
    assert "Authorization" not in page[0].headers
