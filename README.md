# dp-sk-judikatura-grab

Proc-protocol class (Python 3.12, httpx) of the `sk-judikatura` package: downloads Supreme Court (NS SR) decisions from `https://www.nsud.sk/ws/opendata.php`.

- Enumeration by decision-date windows `searchDecision&art_datrozh_od/do` (the documented `getLastDecision` answers 504, the documented `art_datum_*` names are ignored). A list is capped at 1000 ids: such a window is halved down to a day; a day at the cap fails the item with `window_capped`.
- Writes `<dir>/nsud/<id>/decision.json` (raw), `attachment.pdf` (decisions without API text), `fetch.json` (sidecar) and `<dir>/items/<item>.json` (index read by convert). Same version = nothing is fetched again.
- Politeness: at least 0.4 s between requests (never below 0.3), browser User-Agent, three attempts with back-off on 500/502/503/504/timeouts, hosts `www.nsud.sk` and `host.docker.internal` (replay) only, redirects never followed.
- TLS verified; intermediates would be pinned in `src/dp_sk_judikatura_grab/certs/` (none needed today).

Tests: `.venv/Scripts/python -m pytest tests`; `docker build -f Dockerfile.test .` is the ProcMon gate.
