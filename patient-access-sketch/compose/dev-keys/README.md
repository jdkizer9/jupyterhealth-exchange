# Dev-only keys (public on purpose)

Committed so the sketch runs with no setup. They unlock nothing outside this machine: the
deep-link pair signs links only this sketch accepts, and the SMART client pair is registered only
inside the hosted SMART sandbox's launch options (which anyone can construct). Never reuse them.

| file | used for |
|---|---|
| `deeplink-study-dev.pem` / `.pub.pem` | ES256: `mint_deeplink` signs with the private half, `JwtDeepLinkIdentity` verifies with the public half (issuer `study-dev`) |
| `smart-client-dev.pem` / `.pub.pem` | RS384 `private_key_jwt` client assertions to the SMART sandbox; its JWKS is embedded in the sandbox launch options |
