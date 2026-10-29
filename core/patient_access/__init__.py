"""JHE's plugs for the Patient Access Module (sketch, scenario D: the module runs inside JHE).

The module is configured by the PATIENT_ACCESS dict in jhe/settings.py, which names these classes:
    directory.JheOrmDirectory   the module's Directory over JHE's own EhrBrand tables
    identity.JheUserIdentity    the participant is the JHE patient behind the API token
    sink.JheDirectSink          delivers in-process through core.services, no HTTP hop
"""
