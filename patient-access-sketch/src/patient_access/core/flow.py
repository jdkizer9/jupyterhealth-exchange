"""The request-driven half (phases 2-3) as plain functions, so every web layer is a thin adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

import requests

from .credentials import Keyring
from .interfaces import AuthAttemptStore, Directory, IdentityError, IdentityProvider, JobDispatcher, JobStore
from .models import Job, JobId, Participant, Secrets, Source, TypeOutcome
from .pipeline import EHR_TOKEN
from .smart import SmartError, begin, exchange_code


class FlowError(Exception):
    """Safe to show the patient."""


def start_upstream_auth(
    *,
    participant: Participant,
    selection_id: str,
    directory: Directory,
    keyring: Keyring,
    attempts: AuthAttemptStore,
    redirect_uri: str,
    destination: str,
    http: requests.Session,
    clock: Callable[[], datetime],
    scopes: list[str] | None = None,
    scopes_declined: list[str] | None = None,
) -> str:
    try:
        selection = directory.selection(selection_id)
    except KeyError:
        raise FlowError("That organization is not in the directory.") from None
    try:
        settings = directory.settings_for(selection.endpoint)
    except KeyError:
        raise FlowError(f"{selection.brand.name} is not available for connection yet.") from None
    try:
        return begin(
            participant=participant,
            selection=selection,
            settings=settings,
            keyring=keyring,
            redirect_uri=redirect_uri,
            destination=destination,
            attempts=attempts,
            http=http,
            clock=clock,
            scopes=scopes,
            scopes_declined=scopes_declined,
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
    keyring: Keyring,
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
    # Looked up again rather than stored on the attempt: a credential rotated in between is picked up.
    try:
        settings = directory.settings_for(attempt.selection.endpoint)
    except KeyError:
        raise FlowError(f"{attempt.selection.brand.name} is not available for connection yet.") from None
    try:
        token = exchange_code(attempt, code, settings, keyring, http, clock)
    except SmartError as e:
        raise FlowError(str(e)) from e
    try:
        identity.commit(attempt.participant)
    except IdentityError as e:
        raise FlowError(str(e)) from e
    job = Job(
        id="",
        participant=attempt.participant,
        source=Source.from_selection(
            attempt.selection,
            scopes_requested=attempt.scopes,
            scopes_granted=token.scopes_granted,
            ehr_patient_id=token.patient,
            scopes_declined=attempt.scopes_declined,
            auth_method=attempt.auth_method,
            key_id=attempt.key_id,
        ),
        destination=attempt.destination,
        credentials=Secrets({EHR_TOKEN: token.access_token}),
        token_expires_at=token.expires_at,
        outcomes=[
            TypeOutcome(label=q.label, resource_type=q.resource_type, query=q.query)
            for q in settings.queries_for(token.scopes_granted)
        ],
        created_at=clock(),
    )
    job_id = jobs.create(job)
    dispatcher.dispatch(job_id)
    return job_id
