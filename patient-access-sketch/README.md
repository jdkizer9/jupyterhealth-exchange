# Patient Access Module: throwaway sketch

A disposable, end-to-end implementation of the design in
`.notes/uscdi-patient-access-analysis/design-sketch.md` (local notes), built to find out where the
abstractions are wrong before anything real is built. Nothing here ships. See `FINDINGS.md`.

```
docker compose up --build              # db, web (http://localhost:8080), worker
docker compose run --rm web pytest     # core tests + import-linter contracts
```

Layout: `src/patient_access/` is the library (`core/` has no Django and no boto3; the plugs live in
`identity/`, `formatters/`, `destinations/`, `spool/`, `dispatch/`; `django_app/` is the only Django
code). `server/` is the minimal standalone host. `compose/dev-config.json` is the deployment
config: which plugs, their options, vendors and query tables.
