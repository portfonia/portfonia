"""services/vigil/dns_check.py — recipient domain mail-route validation
(issue #454, Vigil R0 P2.1).

#450 Design section 5: "DNS null MX/no valid mail route 422, temporary DNS
failure 503; check off-lock then revalidate submitted normalized values on
commit. DNS proves routing, not mailbox ownership."

Network calls are monkeypatched at the `_domain_has_mail_route` seam — no
real DNS query in this suite.
"""

from __future__ import annotations

import dns.exception
import dns.resolver
import pytest

from app.services.vigil import dns_check
from app.services.vigil.dns_check import (
    VigilDnsUnavailable,
    VigilNoMailRoute,
    check_recipients_dns,
)


def test_valid_domain_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_check, "_domain_has_mail_route", lambda domain: True)
    check_recipients_dns(["a@example.com"])  # must not raise


def test_no_mail_route_raises_no_mail_route(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dns_check, "_domain_has_mail_route", lambda domain: False)
    with pytest.raises(VigilNoMailRoute):
        check_recipients_dns(["a@no-route.example"])


def test_checks_each_unique_domain_once(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake(domain: str) -> bool:
        calls.append(domain)
        return True

    monkeypatch.setattr(dns_check, "_domain_has_mail_route", fake)
    check_recipients_dns(["a@example.com", "b@example.com", "c@other.example"])
    assert sorted(calls) == ["example.com", "other.example"]


def test_transient_dns_failure_propagates_as_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(domain: str) -> bool:
        raise VigilDnsUnavailable("timed out")

    monkeypatch.setattr(dns_check, "_domain_has_mail_route", fake)
    with pytest.raises(VigilDnsUnavailable):
        check_recipients_dns(["a@example.com"])


def test_domain_has_mail_route_null_mx_is_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeExchange:
        def to_text(self) -> str:
            return "."

    class _FakeRdata:
        exchange = _FakeExchange()

    def fake_resolve(domain: str, rdtype: str) -> list[_FakeRdata]:
        assert rdtype == "MX"
        return [_FakeRdata()]

    monkeypatch.setattr(dns_check, "_resolve", fake_resolve)
    assert dns_check._domain_has_mail_route("null-mx.example") is False


def test_domain_has_mail_route_present_mx_is_a_route(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeExchange:
        def to_text(self) -> str:
            return "mail.example.com."

    class _FakeRdata:
        exchange = _FakeExchange()

    def fake_resolve(domain: str, rdtype: str) -> list[_FakeRdata]:
        assert rdtype == "MX"
        return [_FakeRdata()]

    monkeypatch.setattr(dns_check, "_resolve", fake_resolve)
    assert dns_check._domain_has_mail_route("example.com") is True


def test_domain_has_mail_route_falls_back_to_a_record_when_no_mx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_resolve(domain: str, rdtype: str) -> list[str]:
        if rdtype == "MX":
            raise dns.resolver.NoAnswer()  # type: ignore[no-untyped-call]
        if rdtype == "A":
            return ["1.2.3.4"]
        raise dns.resolver.NoAnswer()  # type: ignore[no-untyped-call]

    monkeypatch.setattr(dns_check, "_resolve", fake_resolve)
    assert dns_check._domain_has_mail_route("no-mx-but-a.example") is True


def test_domain_has_mail_route_nxdomain_is_no_route(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolve(domain: str, rdtype: str) -> None:
        raise dns.resolver.NXDOMAIN()  # type: ignore[no-untyped-call]

    monkeypatch.setattr(dns_check, "_resolve", fake_resolve)
    assert dns_check._domain_has_mail_route("does-not-exist.invalid") is False


def test_domain_has_mail_route_timeout_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_resolve(domain: str, rdtype: str) -> None:
        raise dns.exception.Timeout()  # type: ignore[no-untyped-call]

    monkeypatch.setattr(dns_check, "_resolve", fake_resolve)
    with pytest.raises(VigilDnsUnavailable):
        dns_check._domain_has_mail_route("slow.example")
