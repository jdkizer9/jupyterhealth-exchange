"""JheInvitationIdentity: the patient arrives from a JHE invitation link (design §2.2, plan §9 item 5).

The JHE invitation link is `{invitation_url with CODE replaced}`, CODE = "{jhe host}_{token}". The
sketch redeems it server-side exactly as JHE's patient-facing.js does in the browser:
    POST {jhe}/api/v1/invitation/{token}        -> grant {client_id, code, redirect_uri}
    POST {jhe}/o/token/  (PKCE verifier = base64url(token))  -> patient access token
    GET  {jhe}/api/v1/users/profile             -> patient id
`ref` is the JHE patient id; the JHE token goes in `secrets` for the sink. Shortcut vs. the design:
JHE's hub and consent screens are bypassed (the sketch is the whole patient-facing app here).
"""

from __future__ import annotations

import base64
import re
from urllib.parse import unquote

import requests

from patient_access.core.http import session as http_session
from patient_access.core.interfaces import IdentityError, IdentityProvider
from patient_access.core.models import InboundRequest, Participant, Secrets

JHE_TOKEN = "jhe_access_token"
_CODE = re.compile(r"^(?P<host>[^_]+)_(?P<token>[A-Za-z0-9]{32})$")


class JheInvitationIdentity(IdentityProvider):
    name = "jhe_invitation"

    def __init__(self, base_url: str, public_host: str, timeout: float = 20, http: requests.Session | None = None):
        """base_url: how this server reaches JHE (http://jhe:8000). public_host: the host JHE puts in
        invitation codes (localhost:8000); a code naming any other host is refused, so a crafted link
        can't make this server redeem tokens somewhere else."""
        self.base = base_url.rstrip("/")
        self.public_host = public_host
        self.timeout = timeout
        self.http = http or http_session()

    def resolve(self, request: InboundRequest) -> Participant:
        m = _CODE.match(unquote(request.query.get("code", "")))
        if not m:
            raise IdentityError("This invitation link isn't valid.")
        if m.group("host") != self.public_host:
            raise IdentityError("This invitation was issued by a different JupyterHealth Exchange.")
        token = m.group("token")
        try:
            r = self.http.post(f"{self.base}/api/v1/invitation/{token}", timeout=self.timeout)
            if r.status_code in (403, 404, 409, 410):
                is_json = r.headers.get("content-type", "").startswith("application/json")
                detail = (r.json() or {}).get("detail") if is_json else None
                raise IdentityError(detail or "This invitation can no longer be used.")
            r.raise_for_status()
            grant = r.json()["grant"]
            verifier = base64.urlsafe_b64encode(token.encode()).rstrip(b"=").decode()
            t = self.http.post(
                f"{self.base}/o/token/",
                data={
                    "grant_type": "authorization_code",
                    "code": grant["code"],
                    "redirect_uri": grant["redirect_uri"],
                    "client_id": grant["client_id"],
                    "code_verifier": verifier,
                },
                timeout=self.timeout,
            )
            t.raise_for_status()
            access = t.json()["access_token"]
            p = self.http.get(
                f"{self.base}/api/v1/users/profile", headers={"Authorization": f"Bearer {access}"}, timeout=self.timeout
            )
            p.raise_for_status()
            profile = p.json()
        except IdentityError:
            raise
        except (requests.RequestException, ValueError, KeyError) as e:
            raise IdentityError(f"Couldn't confirm your invitation with JupyterHealth ({type(e).__name__}).") from e
        patient = profile.get("patient") or {}
        if not patient.get("id"):
            raise IdentityError("This invitation is not for a patient account.")
        return Participant(
            ref=str(patient["id"]),
            provider=self.name,
            attrs={"jhe_client_id": grant["client_id"], "jhe_host": self.public_host},
            secrets=Secrets({JHE_TOKEN: access}),
        )

    def commit(self, participant: Participant) -> None:
        """JHE's invitation is already spent by redemption; nothing more to do."""
