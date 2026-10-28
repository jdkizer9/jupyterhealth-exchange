"""The module's Django layer, as separately installable apps. A host installs only what it uses,
so it never migrates tables it does not need.

    jobs/       auth attempts, jobs, the claimer, credentials at rest, config wiring, run_jobs.
                Required by any Django host.
    directory/  the module's own brand/location tables and seed_directory. Only for a host
                without a directory of its own (JHE brings its own).
    deeplink/   consumed deep-link ids and mint_deeplink. Only with JwtDeepLinkIdentity.
    portal/     the standalone server's pages (phases 1-3, status). No tables.

`jobs` imports none of the others (import-linter); the config names the optional plugs by
dotted path, and Django refuses to load a model whose app is not installed, so a host that
names a plug from an app it left out fails at startup rather than migrating quietly.
"""
