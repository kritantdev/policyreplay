# PolicyReplay: Workflow Red Team

**The policy said no. The application said yes. PolicyReplay proved it.**

PolicyReplay turns a written business policy into adversarial browser tests, runs them against a disposable
e-commerce app inside a hardened sandbox on Vultr, and uses **deterministic Python** to decide whether the policy
was violated. The LLM interprets and plans; **code proves**. No model output can create a VIOLATION on its own.

- **Live demo:** https://139-84-216-65.sslip.io/
- **Challenge:** Vultr Agent Rush, 01 *Blast Radius Zero* (an agent that operates a browser, with every action inside an isolated sandbox on Vultr)
- **License:** MIT

## What it does

1. **Extract rules.** A Vultr-hosted model reads the policy text and returns structured rules. Every rule is
   checked by code: its `source_clause` must appear verbatim in the policy, its threshold must appear in that clause, and
   its `invariant_type` must come from a fixed list. Anything else is rejected, retried once, then replaced by a cached
   result that the UI labels **"replayed model output"**.
2. **Generate adversarial scenarios.** The model proposes scenarios and browser step plans from a fixed action
   vocabulary (`login`, `goto`, `fill`, `click`, `select`, `expect`). Code validates every plan and **recomputes the
   expected outcome itself**.
3. **Human approval.** Nothing runs until a person presses **Approve**.
4. **Execute in a sandbox.** For each approved scenario, a Playwright browser drives the target app, once on a
   **buggy build** and once on a **correct build** of the same image (`BUG_PROFILE`). The correct build is the
   false-positive control.
5. **Verify with code.** A host-side verifier compares pre-run and post-run database snapshots with the HTTP status the
   browser observed. Verdicts are **VIOLATION**, **PASS** or **INCONCLUSIVE**; it never guesses.
6. **Report and destroy.** An evidence report links every verdict to the policy clause, the browser actions,
   screenshots, observed state and sha256 hashes. The containers and network are then destroyed and the cleanup is logged.

### Policies covered

| ID | Policy | Status |
|---|---|---|
| P-01 | Refunds above INR 10,000 require manager approval (exactly 10,000 needs none) | Tested, planted bug detected |
| P-02 | Only administrators can modify user roles | Tested, planted bug detected |
| P-05 | Customers cannot access another customer's order | Tested, planted bug detected |
| P-03, P-04 | Shipped orders cannot be cancelled; discounts cannot exceed 30% | **Listed as "not tested"**: no verifier yet |

## Verdict rules

| Verdict | Requires |
|---|---|
| **VIOLATION** | Preconditions verified, attempt observed in the HTTP log, and the database shows the invariant broken |
| **PASS** | Preconditions verified, proof the attempt was made (a blocked response), and the database shows the invariant upheld |
| **INCONCLUSIVE** | Anything else: attempt not logged, wrong seed state, unreadable database, or evidence that conflicts |

Controls: the correct build must produce 0 violations; the boundary (exactly 10,000) is encoded and tested; an
administrator changing a role is a positive control; an unreadable database must give INCONCLUSIVE, never PASS.

## Architecture

![Architecture](docs/architecture.svg)

| Layer | Implementation |
|---|---|
| Public entry | Caddy (automatic HTTPS) on the Vultr VM, reverse proxy to the orchestrator |
| Orchestrator + API | FastAPI on a **Vultr Cloud Compute** VM. It holds the inference key and never runs inside a sandbox |
| Inference | **Vultr Serverless Inference** (OpenAI-compatible endpoint), model `minimax-m3.1-flash`, temperature 0 |
| Target app | FastAPI, server-rendered, SQLite, `data-testid` on every control, one image with a `BUG_PROFILE` flag |
| Browser worker | Playwright + Chromium in its own container |
| Verifier | Python on the host, outside the sandbox, reading copied snapshots |
| Dashboard | Single-page HTML + JavaScript served by the API: policy and rules, live run (server-sent events), evidence report |

### Vultr services used

- **Vultr Cloud Compute VM** (control plane, Docker host, public web app) behind a Vultr firewall group
- **Vultr Serverless Inference** for all agent LLM calls (rule extraction and scenario generation)

## Containment

Each run creates a Docker network with `--internal` (no egress) and two containers on it.

```
--read-only  --cap-drop ALL  --security-opt no-new-privileges  --user 10001
--pids-limit  --memory  --cpus   (tmpfs for /tmp and the app's /data)
```

Limits as configured, confirmed from `docker inspect` output recorded in every run's report (`docs/evidence/gate_report.txt`):

| Container | Processes | Memory | CPU | User |
|---|---|---|---|---|
| Target app | 128 | 256 MB | 0.5 | 10001 |
| Playwright worker | 256 | 1 GB | 1.0 | 10001 |

No Docker socket and no API keys inside any container. The only host mounts are the read-only plan file and the run's
evidence directory. Snapshots leave the app container through `docker exec`, and the verifier runs on the host. After
each run both containers and the network are removed and the before/after inventory is recorded.

Tested with `python orchestrator/sandbox_run.py probe`, `stress` and `run` (outputs in `docs/evidence/`). On the Vultr VM, five consecutive full runs through the public dashboard each detected the 3 planted violations with 0 false positives, 0 inconclusive verdicts and confirmed cleanup (`docs/evidence/gate_report.txt`):

| Test | Result |
|---|---|
| Write to the root filesystem | Blocked: read-only file system |
| Reach the internet | Fails: network unreachable |
| Reach `169.254.169.254` | Fails: network unreachable (no route) |
| Secrets in the container environment | None (the only secret-like name is `GPG_KEY`, a public key ID shipped in the official Python image, which the probe whitelists explicitly) |
| Memory hog under a 128 MB limit | Killed, exit code 137, `OOMKilled` true |
| Fork loop under a 32-process limit | Capped: "fork blocked after 31 children" |
| End of run | No containers or run networks left (`docker ps -a`, `docker network ls`) |

### Threat model and honest limits

- **Assumed hostile:** the target application and the browser, not a kernel-exploit attacker. Docker is **not** a complete
  boundary against kernel-level exploits; gVisor or a microVM would be the next step and is not implemented.
- **Chromium's own sandbox is off** (`--no-sandbox`), because it cannot run in a read-only, non-root, capability-free
  container. **The container is the boundary.** We did not add `SYS_ADMIN` to work around it.
- **Audit tables are app-maintained.** P-01, P-02 and P-05 verdicts rely on rows the application under test writes. Each
  verdict also requires the browser-observed HTTP status to agree, and conflicting evidence gives INCONCLUSIVE.
- **A PASS means the tested attempt was blocked.** It is not a compliance certification of the application.
- The demo target is a synthetic app with synthetic data. There is no user-supplied target URL.
- Not built: verifiers for P-03 and P-04, PDF upload, a vision-model review of screenshots.

### Public demo safety

Fixed sample policy and fixed target app only; policy text capped at 4,000 characters; per-IP rate limits; one
concurrent run; a daily cap on model calls (after which cached output is used and labeled); no endpoint returns the key or
any credential.

## Reproduce

Requirements: Ubuntu 24.04 (or similar), Docker, Python 3.12.

```bash
git clone https://github.com/kritantdev/policyreplay.git && cd policyreplay
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt

python orchestrator/sandbox_run.py build     # build the two images
python orchestrator/sandbox_run.py probe     # Chromium + containment probe
python orchestrator/sandbox_run.py stress    # memory hog and fork loop
python orchestrator/sandbox_run.py run       # all cases, buggy vs correct, in the sandbox
```

Run the API and dashboard (copy `.env.example` to `/etc/policyreplay.env`, add your key and model first):

```bash
set -a; source /etc/policyreplay.env; set +a
PR_RUN_MODE=docker uvicorn orchestrator.main:app --host 127.0.0.1 --port 8000
```

Put a reverse proxy with TLS in front for a public deployment. Without a key the system still runs, using labeled
replayed model output.

```bash
python -m pytest verifier/tests worker/tests orchestrator/tests dashboard/tests     # add PR_NO_SANDBOX=1 when running as root
(cd target_app && python -m pytest tests)
python scripts/gate_report.py          # reliability gate and containment evidence from the runs on this machine
```

## Repository layout

```
orchestrator/   FastAPI control plane, LLM client, policy engine, sandbox runner
target_app/     disposable e-commerce app with BUG_PROFILE and its Dockerfile
worker/         Playwright worker (validated step plans only) and its Dockerfile
verifier/       deterministic verifier, schemas, fixtures
dashboard/      single-page UI
policies/       the fixed sample policy
scripts/        model benchmark, auth check, gate report
docs/           architecture diagram and captured evidence
```

## Positioning

PolicyReplay is a workflow tester for e-commerce policies. It combines policy-to-test compilation, adversarial browser
execution and independent, clause-linked verification in one workflow. It is a demonstration and testing tool, not a
compliance certification system, and it does not claim to work on arbitrary web applications.
