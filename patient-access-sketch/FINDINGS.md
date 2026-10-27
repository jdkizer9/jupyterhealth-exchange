# Findings

Running notes, one entry per milestone, written before moving on. What the sketch taught us about
the design; not a changelog.

## Summary: the exit questions (2026-10-22)

| # | question (plan §1) | answer |
|---|---|---|
| Q1 | Do the plugs hold up as real code? | **Yes, with five amendments** (below). Core never imported Django or boto3; three import-linter contracts enforced it on every test run. |
| Q2 | Fetch-all-first with a spool, inside one token? | **Yes, with lots of room.** The largest sandbox patient (2,121 resources) fetched in 11 s against a 1-hour token; 2.8 MB encrypted spool. A kill-mid-fetch test resumed correctly after one bug fix (M8). |
| Q3 | Same worker, config only, S3 and JHE? | **Yes.** Scenario B's config is scenario A's plus two keys; JHE needed zero code changes and accepted 2,121 records. Dispatcher (polling ↔ Dramatiq) and spool (disk ↔ S3) also swap by config alone. |
| Q4 | How much Python is the ported browser flow? | **About 520 lines** for discovery, PKCE, token exchange and the paged pull (`smart.py`, `flow.py`, `http.py`, `puller.py`), inside the report's 400–600 estimate. Documents add 180 more, which the browser never had. |
| Q5 | Document retrieval against a sandbox? | **Half answered.** Inline attachments work end to end; the hosted sandbox has no `Binary` resources, so the download path is unit-tested only. Needs Epic. |
| Q6 | Epic vs the reference sandbox? | **Not answered** (M7 deferred). One difference showed anyway: Epic's `category=clinical-note` filter hides every note on the reference server. |

**Design amendments the code asked for**, in priority order:

1. **A spool must make a type durable before the job may record it as done** (`SpoolHandle.flush`).
   Without it a crash produced a silently incomplete pull (M8 finding 1).
2. **A "referenced resources" pass in phase 4.** 520 references to Practitioner and Organization
   point back into the EHR; the bundle isn't self-contained (M4 finding 1).
3. **JHE's dedupe depends on JHE's own brand directory.** Any iss the module knows but JHE
   doesn't creates a new FhirSource and duplicates the whole pull (M6 finding 2). Decide the
   directory's single home before scenario B ships.
4. **Spending the identity and creating the job must be one transaction**, which core cannot
   express today (M2 finding 1).
5. **Smaller interface changes:** `SinkSession.write(unit, spool)`; `Unit.kind` (not every unit is
   a FHIR Bundle); a `JobClaimer` Protocol with `claim(job_id)` for broker workers;
   `DeepLinkStore.is_consumed`; format and sink as one job status (`delivering`); query tables
   that extend each other; the sweep independent of the dispatcher.
6. **Conformance is now explicit and checked.** Every implementation inherits the Protocol it
   satisfies and `mypy` (with the Django and boto3 stubs) runs in `scripts/check.sh`. Before that,
   nothing verified that a plug matched its Protocol beyond the tests that happened to call it.

**Not covered:** Epic (M7), the `Binary` download path against a real server, C-CDA,
`private_key_jwt` beyond the reference sandbox, a browser run of scenario B, production concerns.

## M0: skeleton (2026-10-22)

Built: package layout, core dataclasses and Protocols, `run_job` with resume/retry/terminal
handling, in-memory implementations of everything, `LocalDiskSpool`, `DbPollingDispatcher` and
`InlineDispatcher`, the Django app with all M0–M3 tables, ORM stores, a polling claimer, config
wiring, `/health`, compose (db, web, worker), three import-linter contracts in pytest.
`scripts/check.sh`: 15 tests, contracts and ruff pass; `docker compose up` gives a healthy web and
a polling worker.

What the code said about the design:

1. **Format and sink are one phase, not two.** The formatter is a generator the sink drains, so
   there is no point where "formatting" is finished and "sinking" has not started. Job status has
   one `delivering` state. The design's six phases are still the right *conceptual* split; the
   job has five states that matter (`queued`, `fetching`, `fetched`, `delivering`, terminal).
2. **`SinkSession.write(unit)` needs the spool.** A unit names its sidecar blobs; for the sink to
   stream them without the formatter loading every PDF into memory, `write` takes
   `(unit, spool)`. Small interface change from design §7.2.
3. **The polling dispatcher needs its own store Protocol.** `claim_next` / `release` /
   `live_spool_refs` are a `JobClaimer` Protocol in `dispatch/db_polling.py`, implemented by the
   Django app with `SELECT … FOR UPDATE SKIP LOCKED`. That is a fourth "Protocol + ORM impl" pair
   the design did not list. The lease columns live on the job row but `JobStore.save` must never
   write them (tested), or a slow worker's save would clear another worker's reclaim.
4. **How a plug gets a Django-implemented store was unspecified.** The wiring fills constructor
   parameters from a small `services` map by name (`deeplink_store`, `site_url`, `vendors`, `run`).
   It works but is a tiny DI container; a real build should decide whether that is acceptable or
   whether each host writes its wiring by hand. `InlineDispatcher` needs `run`, which needs the
   built module: a construction-order wrinkle, solved by building the dispatcher last.
5. **Core never holds an encryption key.** Stores encrypt `Secrets` at rest with the deployment
   key; the spool data key is generated by core with stdlib `secrets` and handed to the spool as
   bytes. Core stays free of `cryptography` (only the spool and the Django app import it).
6. **Naming:** the Django app is `patient_access.django_app` (design §7.4 wiring) rather than
   `patient_access.django` (§9.1). A subpackage named `django` invites import confusion.
7. **Tooling:** import-linter refuses contracts that name modules that don't exist yet, so the
   "boto3 only in S3 modules" contract grows as modules land.

## M1: identity, directory, picker (2026-10-22)

Built: `JwtDeepLinkIdentity` (ES256, issuer-keyed public keys, consumed at `commit`) with
`mint_deeplink`; the directory tables and JHE's Brands Bundle importer (copied, vendor made a
parameter); `seed_directory`, which loads JHE's Epic sample bundle and four SMART sandbox
"brands" whose iss encodes the launch options; `/start`, `/pick`, `/api/brands`, a confirm page
when the link names a brand. Demo (Playwright): minted link → picker → search "sandbox" → Connect
→ selection page showing participant `P-00123`. 18 tests pass.

Copied from elsewhere: the Brands Bundle importer and the sample bundle (JHE), the query table
(JHE's `EHR_PATIENT_PORTAL_PULLS`, as config data), `SandboxConfig` (jupyter-smart-on-fhir).

1. **`DeepLinkStore` needs two methods, not one.** `resolve` must refuse a spent link before the
   patient goes through EHR login, so the store has `is_consumed` (read, on open) as well as
   `consume` (unique insert, on commit). The plug-owns-its-storage pattern held up otherwise: the
   model is in the Django app, the Protocol and an in-memory version are in `identity/`, and core
   never sees either.
2. **`Participant.handle` and `attrs` carry what `commit` needs** (`jti`, link expiry). A
   provider-private field on a core dataclass is a small leak; the alternative is `commit`
   re-parsing the request, which it doesn't have at callback time. Keep the field, document it.
3. **A SMART sandbox "brand" is just an iss.** The hosted launcher reads its whole configuration
   (client type, PKCE, secret, JWKS, preselected patient, skip-login) from a base64url segment in
   the URL, so each auth mode is a directory row and a vendor entry. Cheap and realistic: the
   directory, not a test flag, selects the client auth method, which is how production will work.
   The vendored harness encoded standard base64; the launcher decodes base64url (fixed).
4. **The hosted sandbox is thin on documents.** Of 651 patients, exactly one `DocumentReference`
   exists (inline `text/plain`), there are no `Binary` resources and no `DiagnosticReport.presentedForm`
   in the first 200 reports. M5 can exercise inline attachments only; the Binary path needs Epic
   or a synthetic fixture. Patient `12edb5a0…` (≈1,700 Observations) is the scale test.
5. **fhir-client's `explicitPatient` quirk doesn't apply server-side.** The JS table needed it
   because `patient.request` could not scope Device; the Python puller adds `patient=` to every
   search itself, so the flag is now meaningless. Ported as data, but that row's quirk is moot.

## M2: upstream OAuth (2026-10-22)

Built: `core/smart.py` (discovery, PKCE S256, authorize URL, code exchange with all four client
auth methods, `private_key_jwt` assertions), `core/http.py` (timeouts, GET-only retries honouring
`Retry-After`), `core/flow.py` (`start_upstream_auth`, `finish_upstream_auth`), the ORM
`AuthAttemptStore` with single-use `consume`, `/launch`, `/callback`, `/status/{id}`,
`/api/jobs/{id}`, and `scripts/smoke.py` (browserless, follows the sandbox's redirects).

Demos:
- Browserless, all three client types against the hosted sandbox: public + PKCE, confidential
  symmetric (`client_secret_basic`), confidential asymmetric (`private_key_jwt`, RS384). Each
  authorizes in under a second and creates a `queued` job with 20 granted scopes.
- The sandbox really verifies the assertion: the same flow signed with a random key gets
  `401 Invalid token. invalid signature`.
- Playwright, real UI: link → picker → "confidential asymmetric" → sandbox patient login →
  approve screen listing every requested type → back on `/status/{id}`.

The ported Python for phases 2–3 is about 330 lines (`smart.py` + `flow.py` + `http.py`), replacing
fhir-client.js's discovery, PKCE and token exchange plus four sessionStorage keys.

1. **Spending the identity and creating the job must be atomic, and core cannot say so.**
   `finish_upstream_auth` calls `identity.commit` then `jobs.create`; if the second fails, the link
   is spent with no job. The Django view wraps the call in `transaction.atomic()`, which works
   because both stores share the database, but core has no unit-of-work concept and a host with
   stores in different places would get it wrong silently. A real build should either give core a
   `transaction()` hook in `deps` or document that these two stores must share a transaction.
2. **The callback must not depend on the session.** The `AuthAttempt` carries the participant, so
   a browser that loses its cookie across the EHR round trip (Safari ITP, a different tab) still
   lands. Status visibility then uses "this session created it, or this session's participant
   owns it".
3. **Token lifetime on the sandbox is one hour.** That is the phase-4 budget M3 will measure.
4. **One code path, three auth methods, chosen by the directory.** Swapping client auth needed no
   code change, only which brand the patient picked. `VendorConfig` carrying the method is right.
5. **Dev ergonomics:** the worker caches the module, so it needs autoreload like runserver (added
   with Django's reloader under DEBUG). Config file edits still need a restart of both services.

## M3: fetch to spool (2026-10-22)

Built: `core/puller.py` (query table walk grouped by resource type, paging via `next` links, page
cap, dedupe across rows of the same type, per-type error isolation, 401 aborts, token-expiry
margin, resume per type), the worker running real jobs through `DbPollingDispatcher`, and the
status page polling `/api/jobs/{id}`. Delivery still goes to `MemorySink` until M4.

Measured on the hosted sandbox, patient `12edb5a0…` (the largest found):

| | |
|---|---|
| resources | 2,121 (Labs 1,288, Vital Signs 325, Encounters 243, Diagnostic Reports 158) |
| requests | 56 (sandbox page size 50; Labs alone 26 pages) |
| fetch time | 11.3 s, of a 3,600 s token |
| spool, encrypted | 2.76 MB, about 1.3 KB per resource including Fernet's base64 overhead |

So the token budget is not close to binding for resources, even at 10× this patient; documents
(M5) are what could. The browser-driven job (164 resources) took 3.3 s.

1. **The spool is per resource type, so resume is per type, not per query table row.** Labs and
   Vital Signs both write `Observation`; re-running only the unfinished row would duplicate or
   lose the other row's data. Grouping by type and redoing the whole type on resume is simple and
   correct. The design's "fetch is idempotent per type" was right, more literally than intended.
2. **What to do when the token expires mid-fetch was undecided.** The sketch delivers what it has
   and marks the remaining types `error: authorization expired`, rather than failing the whole
   job. A partial pull with an honest receipt seems better for the patient than nothing; a real
   build should confirm with the consumers.
3. **Dedupe across rows needs only an in-memory id set per type**, as the browser did. No spool
   lookup needed, because each type is fetched in one pass.
4. **OperationOutcome entries inside search bundles are dropped silently** today (filtered by
   `resourceType`). They can carry useful warnings ("some results suppressed"); worth tallying
   into the outcome in a real build.
5. **Ops wrinkles:** `docker compose restart` does not pick up a changed `environment:` (the
   worker kept failing to load config until recreated with `up -d`); Django's reloader runs the
   command in a thread, where `signal.signal` is not allowed.

## M4: format and sink, scenario A complete (2026-10-22)

Built: `CompositionBundleFormatter` (deterministic `urn:uuid` fullUrls, reference rewriting,
Composition with sections, sidecar attachment rewriting ready for M5, a metadata companion),
`LocalDirectorySink`, `S3Sink`, Silo in compose (bucket created on first use),
`scripts/run-smoke-a.sh`. The destination is one env var (`PA_DESTINATION=local|s3`).

Demos:
- Scripted: deep link → sandbox (skip-login brand) → 2,121 resources → `bundle.json` (2.3 MB)
  and `metadata.json`, first under `local/out/`, then in Silo at
  `s3://patient-access/pulls/{ref}/{job}/`. 10.6 s end to end both times. Spool directory empty
  afterwards.
- Playwright: a link minted with `--brand` and `--attrs '{"study":"demo"}'` → confirm page →
  sandbox login → approve → status page shows the receipt with the S3 location; the metadata in
  Silo carries `ref`, `study` and the link id; reopening the link says "already been used".
- The bundle validates as an R4B `Bundle` with `fhir.resources` 7.1.0, the Composition is first,
  and every one of 7,234 internal references resolves to an entry.

1. **The pull is not self-contained.** 520 references point at `Practitioner` (277) and
   `Organization` (243) the query table never fetches; JHE's table doesn't either. The formatter
   makes them absolute EHR URLs, which a consumer can't dereference without a token. A real build
   needs a "referenced resources" pass in phase 4 (collect distinct references to
   Practitioner/Organization/Location/Medication, read each once, scopes permitting). USCDI
   includes provenance-ish data that lives exactly there. **This is a design gap, not a sketch gap.**
2. **`Unit` is not always a FHIR Bundle.** The metadata companion is plain JSON, so units have a
   `kind` ("document", "batch", "metadata") and sinks declare which kinds they accept. The
   Destination pairing check covers it.
3. **The Composition formatter holds the whole bundle in memory**, after two passes over the spool
   (ids, then content). Fine at 2 MB; at 50k resources it would be tens of MB of dicts plus the
   serialised copy. If that matters, `Unit.body` should become a byte stream the formatter writes
   incrementally (JSON streaming), and S3 should use multipart upload. Not needed for the sketch.
4. **Local-directory and S3 sinks are the same sink over two key/value stores.** Both write
   blobs, then the bundle, then metadata last as the completion marker, and abort deletes what they
   wrote. A real build would have one `ObjectStoreSink` with `put`/`delete` backends.
5. **Q3 (same worker, configuration only) holds for A:** switching local ↔ S3 changed one env var.

## M5: documents (2026-10-22)

Built: `core/documents.py`, a second pass in phase 4 over `DocumentReference.content` (one item
per document, by content-type preference) and `DiagnosticReport.presentedForm`; inline `data` and
`Binary` URLs; a server that ignores `Accept` and returns a FHIR `Binary` JSON is unwrapped; count,
per-document and total caps with reasons on the receipt; the token is never sent to a host other
than the FHIR server. The formatter points attachments at `docs/{id}` sidecars and keeps the EHR
URL in an extension; both sinks write the sidecars before the bundle.

Demo: `scripts/run-smoke-a.sh "Express symmetric"` (patient `80a75b5a…`, the only one with a
document): 152 resources plus `docs/DocumentReference-4900007-0` (text/plain, "Patient is a
55-year-old presenting with chest pain.") in Silo, the bundle's attachment rewritten to that key,
`metadata.json` listing it with its sha256. 6.5 s end to end. The Binary-URL, Binary-JSON,
foreign-host and cap paths are covered by unit tests against a fake server only (see 1).

1. **Q5 is only half answered.** The hosted sandbox has no `Binary` resources and no
   `presentedForm`, so the URL download path has never touched a real server. It needs Epic's
   sandbox (M7) or a richer FHIR test server before anyone trusts it.
2. **Epic's query quirks are harmful elsewhere.** `DocumentReference?category=clinical-note`
   returns nothing on the reference server, whose notes have no category. Query tables are
   per-vendor in practice, and most rows are shared, so tables now support
   `{"extends": "uscdi", "override": {...}, "drop": [...]}`. The browser's single table would
   have silently missed every note outside Epic.
3. **Choosing one content item per DocumentReference needs a preference list**, and the right
   order depends on the consumer (a study may want the PDF, a pipeline the text). It is on
   `DocumentPolicy` per vendor today; it probably belongs to the destination instead.
4. **The token-expiry margin check had to be shared** between the resource pages and the
   document downloads (`FhirClient.ensure_valid`), or a long document pass would run past expiry
   and fail with 401s mid-stream.
5. **Empty arrays are invalid FHIR.** The first cut added `"extension": []` to inline attachments;
   a schema validator in the smoke would have caught it. Worth adding to the real build's CI.

## M6: scenario B against the local JHE stack (2026-10-22)

Built: `JheInvitationIdentity` (redeems a JHE invitation server-side: invitation → grant → PKCE
token exchange → profile), `R4BatchFormatter` (100-entry `batch` Bundles per type),
`JheHttpSink` (identifier, `fhir_sources`, `/fhir-import/R4/` with per-entry tally, token revoke),
`compose/dev-config-jhe.json` (extends the base config: only identity and destination change),
`docker-compose.jhe.yml` (joins `jhe_default`, calls `http://jhe:8000`), and
`scripts/jhe-setup.sh`, which adds one JHE client "Patient Access Sketch" with
`invitation_url = http://localhost:8080/start?code=CODE` and issues an invitation for the seeded
patient Peter. **No JHE code changed.**

Demo: JHE invitation link → sketch `/start` (redeemed server-side) → picker → sandbox (skip-login
brand) → worker → JHE. 2,121 records accepted into FhirSource 90004 for JHE patient 40001,
0 rejected, 68 R4→R5 conversion warnings tallied by reason (dropped `Encounter.reasonCode`,
`Procedure.reasonReference`), the sandbox patient id attached as a JHE `PatientIdentifier`, the
patient's JHE token revoked at the end. 20.7 s. JHE's DB shows 2,121 `FhirAuxResource` rows on
that source. Same worker image and code as scenario A; the deployment config differs in two keys.

1. **Q3 holds: same worker, configuration only, two very different destinations.** The config
   for B is A's config plus `identity` and `destination`, via a one-line `extends`.
2. **JHE's dedupe depends on JHE's directory, which the module now owns.** A second pull created
   a *new* FhirSource (90005) and stored all 2,121 records again instead of 409-ing them: JHE
   reuses a source only when `ehr_base_url` matches a brand in *its own* `EhrBrand` table, and
   "a source that resolves to no brand always creates". The sandbox iss isn't in JHE's table.
   With real Epic brands (imported into both) it would reuse, but the coupling is real: when the
   module owns the directory and JHE keeps its own, any brand only the module knows duplicates on
   every pull. Fix options: mounted mode shares the tables (design §9.1's migration), or the JHE
   sink sends the brand and JHE resolves source by (patient, data source, iss) instead of brand.
   The 409 duplicate path itself was therefore not exercised end to end (unit-tested only).
3. **The JHE sink must read the JHE identity's secret.** `destinations/jhe.py` imports
   `JHE_TOKEN` from `identity/jhe_invitation.py`. Identity and sink are coupled the same way
   formatter and sink are; a real build should make that pairing explicit (or name the secret in
   config) rather than share a constant.
4. **Redemption happens on open, not on commit.** JHE spends the invitation when `resolve` calls
   it; JHE's own 12-hour "recently redeemed" window is what makes reopening work. Different from
   the deep link's "spend at commit", and owned by JHE, not the module. Fine, but the two
   identity providers have different single-use semantics, which the receipt should say.
5. **Compose networks collide on service names.** Joining `jhe_default` made `db` resolve to JHE's
   Postgres (password failure). The sketch now addresses its own services by aliases (`pa-db`,
   `pa-s3`). Any host that runs the module beside JHE on one network has the same trap.
6. **Revoking the JHE token at finish conflicts with retry.** If delivery fails after `finish`
   revoked the token, a retry can't post. `abort` keeps it; `finish` only runs on success, so it
   is right today, but a partial-success-then-retry design would need a refreshable credential.

## M7: Epic sandbox (skipped, 2026-10-22)

Not attempted: it needs a new Epic app registration, which is deferred (plan §7 item 2). Q6 and
the real `Binary` path (M5 finding 1) stay open until then.

## M8: S3 staging spool and crash test (2026-10-22)

Built: `S3StagingSpool` (per-type NDJSON chunks, each object Fernet-encrypted with the job's key;
blobs and blob index likewise; a bucket lifecycle rule `expire-staging`, 1 day on `staging/`;
`delete` and `sweep` by prefix), `compose/dev-config-s3spool.json` (extends the base config, swaps
only `spool`), and an integration test against Silo.

Demos:
1. **Kill the worker mid-fetch (S3 spool).** After 7 of 18 query rows finished, `docker compose
   kill worker`. Staging held 5 encrypted chunks (the 5 non-empty finished types), all opaque
   Fernet tokens. With the lease expired, the restarted worker resumed: attempt 2 skipped the
   finished types (49 requests instead of 56), delivered all 2,121 resources, and emptied staging.
   The same test passed on the local-disk spool.
2. **Orphan, crypto-shredded.** A job ended while its staging objects remained: its key gone from
   the row, the objects fail with `InvalidToken` under any other key, and `sweep` (keep = spools of
   live jobs) removed them. An orphan local-disk directory from a test that never stored its key
   was swept the same way.
3. **The lifecycle backstop is real on Silo.** With a past-dated expiration rule on a probe prefix,
   a write there returns 200 and is gone from listings immediately; a control object elsewhere
   stays. The 1-day rule on `staging/` can't be watched in a session, but the engine enforcing it
   is live, as MinIO's was.
4. **Audit.** Of 15 terminal jobs, none holds credentials, participant secrets or (after fixing a
   hand-edited row) a spool ref.

1. **The crash test found a real bug: an "ok" outcome over data that was never written.** Both
   spools buffer (Python file buffers on disk; in-memory chunks for S3). The puller saved a type's
   outcome as `ok` while its resources were still buffered, so a crash lost them *and* resume
   skipped them, delivering a silently incomplete pull. Fix: `SpoolHandle.flush(type)` is now
   part of the Protocol, and the puller calls it before saving the outcome (local disk also
   `fsync`s). **The design needs this rule written down: a spool must make a type durable before
   the job may say it is done.** No unit test with in-memory plugs could have found it.
2. **S3 can't append**, so the S3 spool is chunked. That is invisible to core because resume is
   already per type (M3 finding 1), which is what made the chunked layout safe.
3. **Ending a job by hand bypasses crypto-shred bookkeeping.** My manual "abandon" edit left a
   `spool_ref` behind. A real build needs an operator command that ends a job through core
   (`_finish`), not ad hoc SQL.
4. **Which spool a process uses is deployment config, and processes can disagree.** One `up`
   without the env var silently gave web and worker the local-disk spool while I believed they
   used S3. Harmless here (only the worker touches the spool), but a host that splits fetch and
   delivery across processes must pin one spool config, or the spool should record its kind on
   the job.
5. **Lease length is the resume latency.** A crashed job waits out its 10-minute lease before
   another worker reclaims it. Fine for a background pull, but the status page shows "fetching"
   the whole time; a heartbeat-renewed short lease would show the stall sooner.

## Stretch: DramatiqDispatcher on Redis (2026-10-22)

Built: `dispatch/dramatiq_dispatcher.py` (dispatch sends the job id; the actor calls a registered
runner), `django_app/dramatiq_worker.py` (the worker-side entry: Django setup, build the module,
claim the job by id through the same lease, run, release), `OrmJobClaimer.claim(job_id)`, and
`docker-compose.dramatiq.yml` (adds Redis, points `PA_CONFIG` at a config whose only change is
`dispatcher`, and runs `dramatiq …` as the worker command). 88 lines in all; **no change to core,
the plugs, the views or the stores' Protocols.**

Demo: scenario A smoke with the override: web logged "sent to dramatiq queue", the Dramatiq worker
fetched 2,121 resources and delivered them to Silo. 10.5 s, same as the polling worker.

1. **The `JobDispatcher` seam held.** Swapping the execution model was one config key plus the
   host's worker command, which is the claim design §7.2 made.
2. **The worker side needs a claim by id, not just `claim_next`.** A broker delivers a specific
   job, possibly twice, and may run beside a polling worker; leasing that id (and skipping if
   leased or terminal) keeps "at most one runner per job" true under every dispatcher. The claimer
   Protocol grows from three methods to four.
3. **Lost messages are not recovered yet.** The invariant "the row is the system of record; a lost
   message is recovered by the sweep" needs a periodic re-dispatch of jobs that are `queued` (or
   `failed` and due) with no lease and no recent activity. With polling, the poll *is* that
   sweep; with a broker, something must still run it (cron, or a Dramatiq periodic actor). The
   design should say the sweep is independent of the dispatcher.
4. **Broker libraries configure at import time.** Dramatiq binds actors to a global broker when
   the module is imported, so the broker URL comes from the environment, not the plug's options,
   and the runner is registered from the host's worker entry. The module also can't be named
   `dramatiq.py` inside `dispatch/` without shadowing the library.

## Post-sketch: a database restart killed the polling worker silently (2026-10-26)

Found during a walkthrough three days after the last run: a job sat `queued` with "waiting for a
worker". Postgres had gone through crash recovery (the host slept; `database system was not
properly shut down`), the worker's claim query raised, the poll loop thread died, and the
container stayed "Up" because Django's dev reloader kept the main thread alive. The web process
reconnects per request, so only the worker was affected, and nothing reported it.

Fixed: the loop catches infrastructure failures around claim and sweep, logs, backs off
(2 s, 4 s, ... up to 60 s) and keeps polling; it calls a `heartbeat` on every healthy iteration;
`run_jobs --heartbeat-file` touches a file the compose healthcheck watches, so a dead or stuck loop
shows as unhealthy even while the process runs. Unit-tested with a claimer that fails like a
database in recovery.

1. **"The row is the queue" needs a live poller, and nothing checked that it was alive.** Any
   worker-side entry point (polling, Dramatiq, cron) needs a liveness signal the host can act on;
   with Dramatiq the broker's own health is not the worker's.
2. **A loop that owns the process must never let an infrastructure exception escape**, and a
   dev reloader can mask exactly that. Production runs without the reloader, where the process
   would at least have exited and been restarted; the healthcheck covers both.
3. Compose does not restart unhealthy containers by itself; a real host (ECS, Kubernetes, Fly)
   does, which is why the healthcheck is the right primitive.

## Post-sketch: deploy mid-pull, rehearsed (2026-10-27)

Setup: web on `ThreadDispatcher`, worker container stopped. A pull was started and the web
container restarted after 6 of 18 query rows had finished, standing in for a deploy. The thread's
lease was released at exit; `run_jobs --once` (the cron form) then resumed the job on its second
attempt and delivered all 2,121 resources. The spool survived here because it is a bind mount;
on Fly it would not, which is what the new resume check is for.

1. **`run_jobs` refused to be the sweep under a non-polling dispatcher.** It checked for the
   polling dispatcher and errored under `ThreadDispatcher`, contradicting "the sweep is independent
   of the dispatcher". Fixed: any dispatcher, with polling defaults. The first rehearsal found it.
2. **Resume must verify the spool, not the row** (pipeline `_spool_intact`): a type marked done but
   missing from the spool is refetched under a live token; a finished fetch with a lost spool fails
   with "connect again". Without this a redeploy on Fly would have delivered a partial bundle.
3. **Leases are released at process exit** (`atexit` in `ThreadDispatcher`), so the sweep resumes on
   its next tick rather than after the 10-minute lease. Under Django's dev reloader the release is
   racy (the reloader parent is killed first); under gunicorn's graceful stop it runs normally.
