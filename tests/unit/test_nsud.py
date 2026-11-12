"""The nsud OpenData client: query strings, politeness (pace, retries), allow-list, attachment path guard, TLS pin loader."""
from __future__ import annotations

import json
import ssl
from datetime import date
from pathlib import Path
from unittest import mock

import httpx
import pytest

from conftest import PDF_BYTES, FakeTime, Server, fixture_bytes
from dp_sk_judikatura_grab import http
from dp_sk_judikatura_grab.errors import CONFIG_ERROR, TRANSIENT_ERROR, GrabError
from dp_sk_judikatura_grab.nsud import MIN_PAUSE, NsudClient, Pacer

API = "https://www.nsud.sk/ws/opendata.php"


def make(route: httpx.Response | list[httpx.Response] | None = None, delay: float = 0.4, base: str = "https://www.nsud.sk") -> tuple[NsudClient, Server, FakeTime]:
    fake = FakeTime()
    answers = [route] if isinstance(route, httpx.Response) else list(route or [])

    def handle(_request: httpx.Request) -> httpx.Response:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    server = Server(handle, fake)
    client = http.build_client(http.DEFAULT_ALLOWED_HOSTS, server.transport())
    return NsudClient(client, Pacer(delay, clock=fake.clock, sleep=fake.sleep), base, sleep=fake.sleep), server, fake


@pytest.mark.unit
def test_the_search_query_string_uses_the_real_parameter_names_not_the_documented_ones() -> None:
    # P81-06: the OpenData page documents art_datum_od/do, the live API ignores them; the real names are art_datrozh_od/do
    nsud, server, _ = make(httpx.Response(200, content=fixture_bytes("search-2026-09.json")))

    nsud.search_ids(date(2026, 9, 1), date(2026, 9, 30))

    assert server.urls() == [f"{API}?searchDecision&art_datrozh_od=2026-09-01&art_datrozh_do=2026-09-30"]
    assert "art_datum" not in server.urls()[0]
    assert "getLastDecision" not in server.urls()[0]


@pytest.mark.unit
def test_the_27_id_answer_of_september_2026_is_parsed() -> None:
    nsud, _, _ = make(httpx.Response(200, content=fixture_bytes("search-2026-09.json")))

    ids = nsud.search_ids(date(2026, 9, 1), date(2026, 9, 30))

    assert len(ids) == 27
    assert all(i.isdigit() for i in ids)


@pytest.mark.unit
def test_non_digit_ids_are_dropped_including_path_traversal() -> None:
    body = json.dumps(["150000", "../../etc/passwd", "12a", 177749, "", None, "9 9", "٣"])
    nsud, _, _ = make(httpx.Response(200, text=body))

    assert nsud.search_ids(date(2026, 1, 1), date(2026, 1, 31)) == ["150000", "177749"]


@pytest.mark.unit
def test_a_body_that_is_not_a_json_array_gives_no_ids() -> None:
    for body in ("", "   ", "not json", '{"a":1}', "null"):
        nsud, _, _ = make(httpx.Response(200, text=body))
        assert nsud.search_ids(date(2026, 1, 1), date(2026, 1, 31)) == []


@pytest.mark.unit
def test_get_decision_returns_the_raw_body_verbatim() -> None:
    raw = fixture_bytes("decision-150000.json")
    nsud, server, _ = make(httpx.Response(200, content=raw))

    assert nsud.get_decision("150000") == raw
    assert server.urls() == [f"{API}?getDecision&id=150000"]


@pytest.mark.unit
def test_a_missing_decision_answers_an_empty_list_and_gives_none() -> None:
    nsud, _, _ = make(httpx.Response(200, content=fixture_bytes("decision-missing.json")))

    assert nsud.get_decision("999999999") is None


@pytest.mark.unit
@pytest.mark.parametrize("bad", ["../x", "12a", "", "1 2", "1/2"])
def test_get_decision_refuses_an_id_that_is_not_digits(bad: str) -> None:
    nsud, server, _ = make(httpx.Response(200, text="[]"))

    with pytest.raises(GrabError) as caught:
        nsud.get_decision(bad)

    assert caught.value.code == CONFIG_ERROR
    assert server.requests == []


@pytest.mark.unit
def test_a_500_is_retried_and_the_second_answer_is_used() -> None:
    nsud, server, fake = make([httpx.Response(500, text="<html>Internal Server Error</html>"), httpx.Response(200, content=fixture_bytes("search-2026-09.json"))])

    assert len(nsud.search_ids(date(2026, 9, 1), date(2026, 9, 30))) == 27
    assert len(server.requests) == 2
    assert any(s >= 1 for s in fake.sleeps), "a back-off pause separates the attempts"


@pytest.mark.unit
@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_three_failing_attempts_end_as_a_transient_error(status: int) -> None:
    nsud, server, _ = make(httpx.Response(status, text="Gateway Time-out"))

    with pytest.raises(GrabError) as caught:
        nsud.search_ids(date(2026, 9, 1), date(2026, 9, 30))

    assert caught.value.code == TRANSIENT_ERROR
    assert len(server.requests) == 3


@pytest.mark.unit
def test_a_timeout_is_retried_like_a_5xx() -> None:
    fake = FakeTime()
    calls: list[int] = []

    def route(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, text="[]")

    client = http.build_client(http.DEFAULT_ALLOWED_HOSTS, httpx.MockTransport(route))
    nsud = NsudClient(client, Pacer(0.4, clock=fake.clock, sleep=fake.sleep), "https://www.nsud.sk", sleep=fake.sleep)

    assert nsud.get_decision("1") is None
    assert len(calls) == 3


@pytest.mark.unit
def test_a_404_is_a_config_error_and_is_not_retried() -> None:
    nsud, server, _ = make(httpx.Response(404, text="not found"))

    with pytest.raises(GrabError) as caught:
        nsud.get_decision("1")

    assert caught.value.code == CONFIG_ERROR
    assert len(server.requests) == 1


@pytest.mark.unit
def test_calls_are_paced_by_at_least_the_delay() -> None:
    nsud, server, _ = make(httpx.Response(200, text="[]"), delay=0.4)

    for decision_id in ("1", "2", "3"):
        nsud.get_decision(decision_id)

    gaps = [b.at - a.at for a, b in zip(server.requests, server.requests[1:], strict=False)]
    assert all(gap >= 0.4 - 1e-6 for gap in gaps), gaps


@pytest.mark.unit
def test_the_delay_cannot_be_set_below_the_politeness_floor() -> None:
    assert MIN_PAUSE >= 0.3
    nsud, server, _ = make(httpx.Response(200, text="[]"), delay=0.0)

    nsud.get_decision("1")
    nsud.get_decision("2")

    assert server.requests[1].at - server.requests[0].at >= MIN_PAUSE - 1e-6


@pytest.mark.unit
def test_an_attachment_is_downloaded_from_the_data_att_path() -> None:
    nsud, server, _ = make(httpx.Response(200, content=PDF_BYTES, headers={"content-type": "application/pdf"}))

    assert nsud.download_attachment("9c8/581950.03aab9.pdf") == PDF_BYTES
    assert server.urls() == ["https://www.nsud.sk/data/att/9c8/581950.03aab9.pdf"]


@pytest.mark.unit
@pytest.mark.parametrize("subor", ["../etc/passwd", "a/../b.pdf", "abc.pdf", "a/b/c.pdf", "/abs/x.pdf", "a/x.exe", "a/x..pdf", "a/b.pdf?x=1", "", "a/ b.pdf", "https://evil.test/x.pdf"])
def test_an_attachment_path_that_is_not_the_expected_shape_is_refused(subor: str) -> None:
    nsud, server, _ = make(httpx.Response(200, content=PDF_BYTES))

    with pytest.raises(GrabError) as caught:
        nsud.download_attachment(subor)

    assert caught.value.code == CONFIG_ERROR
    assert server.requests == []


@pytest.mark.unit
def test_an_attachment_that_is_not_a_pdf_is_refused() -> None:
    nsud, _, _ = make(httpx.Response(200, text="<html>login</html>"))

    with pytest.raises(GrabError) as caught:
        nsud.download_attachment("a/b.pdf")

    assert caught.value.code == CONFIG_ERROR


@pytest.mark.unit
def test_the_client_refuses_a_host_that_is_not_allowed() -> None:
    with pytest.raises(GrabError) as caught:
        make(httpx.Response(200, text="[]"), base="https://evil.example")

    assert caught.value.code == CONFIG_ERROR


@pytest.mark.unit
def test_a_replay_server_on_host_docker_internal_is_allowed() -> None:
    nsud, server, _ = make(httpx.Response(200, text="[]"), base="http://host.docker.internal:8765")

    nsud.get_decision("1")

    assert server.urls() == ["http://host.docker.internal:8765/ws/opendata.php?getDecision&id=1"]


@pytest.mark.unit
def test_redirects_are_never_followed() -> None:
    nsud, server, _ = make(httpx.Response(302, headers={"location": "https://evil.example/"}))

    with pytest.raises(GrabError) as caught:
        nsud.get_decision("1")

    assert caught.value.code == CONFIG_ERROR
    assert len(server.requests) == 1


@pytest.mark.unit
def test_the_ssl_context_is_built_from_certifi_plus_every_pem_in_the_certs_directory(tmp_path: Path) -> None:
    (tmp_path / "b.pem").write_text("x", encoding="ascii")
    (tmp_path / "a.pem").write_text("x", encoding="ascii")
    (tmp_path / "README.md").write_text("not a certificate", encoding="ascii")

    with mock.patch.object(ssl.SSLContext, "load_verify_locations", autospec=True) as loader:
        context = http.ssl_context(verify_tls=True, certs_dir=tmp_path)

    loaded = [str(call.kwargs.get("cafile")) for call in loader.call_args_list]
    assert loaded[-2:] == [str(tmp_path / "a.pem"), str(tmp_path / "b.pem")]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert not any("README" in item for item in loaded)


@pytest.mark.unit
def test_the_shipped_certs_directory_is_empty_of_pins_and_documented() -> None:
    assert list(http.CERTS_DIR.glob("*.pem")) == []
    assert "no intermediate" in (http.CERTS_DIR / "README.md").read_text(encoding="utf-8").lower()


@pytest.mark.unit
def test_verify_tls_false_gives_a_non_verifying_context() -> None:
    context = http.ssl_context(verify_tls=False)

    assert context.verify_mode == ssl.CERT_NONE
