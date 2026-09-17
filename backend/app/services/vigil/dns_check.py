"""Recipient domain mail-route validation (issue #454, Vigil R0 P2.1).

#450 Design section 5: "DNS null MX/no valid mail route 422, temporary DNS
failure 503; check off-lock then revalidate submitted normalized values on
commit. DNS proves routing, not mailbox ownership." Runs outside any
transaction/lock — it's a network call, and Design section 3 forbids
network calls while holding business locks.
"""

from __future__ import annotations

from collections.abc import Iterable

import dns.exception
import dns.resolver

_TIMEOUT_SECONDS = 5.0


class VigilDnsUnavailable(RuntimeError):
    """Transient DNS failure (-> 503, no state change)."""


class VigilNoMailRoute(RuntimeError):
    """No usable MX (or implicit A/AAAA) route for a recipient domain
    (-> 422). Never means the mailbox itself exists."""


def _resolve(domain: str, rdtype: str) -> object:
    return dns.resolver.resolve(domain, rdtype, lifetime=_TIMEOUT_SECONDS)


def _domain_has_mail_route(domain: str) -> bool:
    try:
        answer = _resolve(domain, "MX")
    except dns.resolver.NoAnswer:
        answer = None
    except dns.resolver.NXDOMAIN:
        return False
    except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
        raise VigilDnsUnavailable(f"DNS lookup for {domain!r} was unavailable") from exc

    if answer is not None:
        # RFC 7505 null MX: a lone MX record pointing at the root (".")
        # explicitly declares "this domain accepts no mail" — not a route.
        exchanges = [rdata.exchange.to_text().rstrip(".") for rdata in answer]  # type: ignore[attr-defined]
        return not (exchanges and all(e == "" for e in exchanges))

    # No MX record — RFC 5321 §5.1 implicit-MX fallback to A/AAAA.
    for rdtype in ("A", "AAAA"):
        try:
            _resolve(domain, rdtype)
            return True
        except dns.resolver.NoAnswer:
            continue
        except dns.resolver.NXDOMAIN:
            return False
        except (dns.exception.Timeout, dns.resolver.NoNameservers) as exc:
            raise VigilDnsUnavailable(f"DNS lookup for {domain!r} was unavailable") from exc
    return False


def check_recipients_dns(emails: Iterable[str]) -> None:
    """Raise VigilNoMailRoute (422) or VigilDnsUnavailable (503) if any
    recipient's domain doesn't resolve to a usable mail route. Each unique
    domain is checked once regardless of how many recipients share it."""
    domains = {email.rpartition("@")[2] for email in emails}
    for domain in domains:
        if not _domain_has_mail_route(domain):
            raise VigilNoMailRoute(f"no valid mail route for domain {domain!r}")
