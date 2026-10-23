"""The request-driven half (phases 2-3) as plain functions, so every web layer is a thin adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

import requests

from .interfaces import AuthAttemptStore, Directory, IdentityError, IdentityProvider, JobDispatcher, JobStore
from .models import Job, JobId, Participant, Secrets, Source, TypeOutcome
from .pipeline import EHR_TOKEN
from .smart import SmartError, begin, exchange_code
from .vendors import VendorConfig


class FlowError(Exception):
    """Safe to show the patient."""


def start_upstream_auth(
    *,
    participant: Participant,
    location_id: str,
    directory: Directory,
    vendors: Mapping[str, VendorConfig],
    attempts: AuthAttemptStore,
    redirect_uri: str,
    destination: str,
    http: requests.Session,
    clock: Callable[[], datetime],
    scopes: list[str] | None = None,
) -> str:
    try:
        location = directory.location(location_id)
    except KeyError:
        raise FlowError("That organization is not in the directory.") from None
    vendor = vendors.get(location.vendor)
    if vendor is None:
        raise FlowError(f"{location.brand_name} is not available for connection yet.")
    try:
        return begin(
            participant=participant,
            location=location,
            vendor=vendor,
            redirect_uri=redirect_uri,
            destination=destination,
            attempts=attempts,
            http=http,
            clock=clock,
            scopes=scopes,
        )
    except SmartError as e:
        raise FlowError(str(e)) from e


def finish_upstream_auth(
    *,
    state: str,
    code: str | None,
    error: str | None,
    error_description: str | None,
    directory: Directory,
    vendors: Mapping[str, VendorConfig],
    attempts: AuthAttemptStore,
    identity: IdentityProvider,
    jobs: JobStore,
    dispatcher: JobDispatcher,
    http: requests.Session,
    clock: Callable[[], datetime],
) -> JobId:
    """Callback: consume the attempt, exchange the code, spend the identity, create and dispatch the job."""
    try:
        attempt = attempts.consume(state)
    except KeyError:
        raise FlowError("This sign-in has expired or was already used. Please start again from your link.") from None
    if error:
        raise FlowError(f"The organization did not complete sign-in: {error_description or error}.")
    if not code:
        raise FlowError("The organization did not return an authorization code.")
    vendor = vendors[attempt.vendor]
    try:
        token = exchange_code(attempt, code, vendor, http, clock)
    except SmartError as e:
        raise FlowError(str(e)) from e
    try:
        identity.commit(attempt.participant)
    except IdentityError as e:
        raise FlowError(str(e)) from e
    location = directory.location(attempt.location_id)
    job = Job(
        id="",
        participant=attempt.participant,
        source=Source(
            vendor=vendor.name,
            iss=attempt.iss,
            brand_name=location.brand_name,
            location_id=location.id,
            location_name=location.name,
            scopes_requested=attempt.scopes,
            scopes_granted=token.scopes_granted,
            ehr_patient_id=token.patient,
            brand_id=location.brand_id,
            brand_npi=location.brand_npi,
        ),
        destination=attempt.destination,
        credentials=Secrets({EHR_TOKEN: token.access_token}),
        token_expires_at=token.expires_at,
        outcomes=[
            TypeOutcome(label=q.label, resource_type=q.resource_type, query=q.query)
            for q in vendor.queries_for(token.scopes_granted)
        ],
        created_at=clock(),
    )
    job_id = jobs.create(job)
    dispatcher.dispatch(job_id)
    return job_id
