# Pinned intermediate certificates

The Supreme Court host `www.nsud.sk` sends its full chain (checked 2026-11-12 from the host and from a container on the ProcMon network: no intermediate is missing), so no intermediate is pinned today and this directory holds no `*.pem` file. `http.ssl_context()` loads certifi plus every `*.pem` found here, so a missing intermediate (the phase 79b defect of def.finanze.it) is fixed by adding the file, its source URL and SHA-256 fingerprint below and releasing a new tag. `verifyTls=false` is for replay servers only.
