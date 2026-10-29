"""Deep-link identity: verify on open, spend on commit, never on open."""

import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from patient_access.core.interfaces import IdentityError
from patient_access.core.models import InboundRequest
from patient_access.identity.jwt_deeplink import InMemoryDeepLinkStore, JwtDeepLinkIdentity, mint_link, parse_ttl

KEYS = Path(__file__).resolve().parent.parent / "compose" / "dev-keys"
PRIV = str(KEYS / "deeplink-study-dev.pem")
PUB = str(KEYS / "deeplink-study-dev.pub.pem")


def _token(**kw):
    url = mint_link("http://x", PRIV, kw.pop("iss", "study-dev"), kw.pop("ref", "P-1"), **kw)
    return parse_qs(urlparse(url).query)["t"][0]


def _idp(store=None):
    return JwtDeepLinkIdentity(issuers={"study-dev": PUB}, deeplink_store=store or InMemoryDeepLinkStore())


def test_open_does_not_spend_commit_does():
    idp = _idp()
    t = _token(brand="42", attrs={"arm": "A"})
    p = idp.resolve(InboundRequest(query={"t": t}))
    assert p.ref == "P-1" and p.attrs["brand"] == "42" and p.attrs["arm"] == "A"
    assert idp.resolve(InboundRequest(query={"t": t})).ref == "P-1"  # reopen is fine
    idp.commit(p)
    with pytest.raises(IdentityError, match="already been used"):
        idp.resolve(InboundRequest(query={"t": t}))
    with pytest.raises(IdentityError, match="already been used"):
        idp.commit(p)  # a racing second callback loses


def test_rejects_expired_unknown_issuer_and_tampered():
    idp = _idp()
    with pytest.raises(IdentityError, match="expired"):
        idp.resolve(InboundRequest(query={"t": _token(now=int(time.time()) - 3600, ttl_seconds=60)}))
    with pytest.raises(IdentityError, match="recognized"):
        idp.resolve(InboundRequest(query={"t": _token(iss="someone-else")}))
    t = _token()
    head, body, sig = t.split(".")
    with pytest.raises(IdentityError, match="not valid"):
        idp.resolve(InboundRequest(query={"t": f"{head}.{body}.{sig[:-4]}AAAA"}))
    with pytest.raises(IdentityError):
        idp.resolve(InboundRequest(query={}))


def test_reserved_attrs_and_ttl_parsing():
    with pytest.raises(ValueError):
        mint_link("http://x", PRIV, "study-dev", "P", attrs={"sub": "evil"})
    assert parse_ttl("7d") == 7 * 86400 and parse_ttl("30m") == 1800
