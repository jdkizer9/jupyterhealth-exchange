# Patient Access Module: throwaway sketch

A disposable, end-to-end implementation of the design in
`.notes/uscdi-patient-access-analysis/design-sketch.md` (local notes), built to find out where the
abstractions are wrong before anything real is built. Nothing here ships. **Read `FINDINGS.md`
first**; its summary answers the plan's exit questions.

## Run it

```
docker compose up -d --build                 # db, s3 (Silo), web on :8080, worker
scripts/check.sh                             # tests, import-linter contracts, ruff (in containers)
docker compose exec web python manage.py mint_deeplink --ref P-001   # a patient link
scripts/run-smoke-a.sh                       # scenario A end to end, no browser
```

The EHR is the hosted SMART Health IT sandbox. Directory entries named "Express" skip its login
screens for scripted runs; the others show the real login and approve screens.

| variant | how |
|---|---|
| deliver to `local/out/` instead of Silo | `PA_DESTINATION=local docker compose up -d` |
| spool in Silo instead of local disk | `PA_VARIANT=s3spool docker compose up -d` |
| Dramatiq + Redis instead of DB polling | `docker compose -f docker-compose.yml -f docker-compose.dramatiq.yml up -d --build` |
| scenario B: deliver into the JHE dev stack | start JHE (repo root `docker compose up -d`), run `scripts/jhe-setup.sh`, `export PA_JHE_DATA_SOURCE_ID=<printed id>`, then `docker compose -f docker-compose.yml -f docker-compose.jhe.yml up -d` and open the printed invitation link |

Silo's console is at http://localhost:9001 (`patient-access-dev` / `patient-access-dev-secret`).
Every value in the compose files and `compose/` is a public, local-only dev value.

## Layout

`src/patient_access/` is the library. `core/` has no Django and no boto3. The plugs live in
`identity/`, `formatters/`, `destinations/`, `spool/` and `dispatch/`, and `testing/` holds
in-memory versions of everything. `django_apps/` is the only Django code, split into separately
installable apps (`jobs` required; `directory`, `deeplink`, `portal` optional) so a host installs
only the tables it uses. `server/` is the minimal standalone host, which installs all four. its `PATIENT_ACCESS` settings dict (plugs, options, vendors) is built by
`server/patient_access_config.py` per `PA_VARIANT`, the way a reusable Django app is configured.
