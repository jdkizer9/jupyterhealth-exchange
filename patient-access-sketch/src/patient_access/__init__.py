"""Patient Access Module (throwaway sketch).

Layers (design-sketch.md §9):
    core/          pure Python: dataclasses, Protocols, SMART client, puller, run_job
    identity/      IdentityProvider plugs
    formatters/    Formatter plugs
    destinations/  Sink plugs and the Destination pairs
    spool/         Spool plugs
    dispatch/      JobDispatcher plugs
    testing/       in-memory implementations of every Protocol
    django_apps/   jobs, directory, deeplink, portal: separately installable Django apps (the only Django code)
"""
