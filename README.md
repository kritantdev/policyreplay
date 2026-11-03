# PolicyReplay: Workflow Red Team

Converts written business policies into adversarial browser tests, runs them
against a disposable e-commerce app inside a sandbox on Vultr, and uses
deterministic Python to decide whether a policy was violated.

**The LLM interprets and plans; code proves.**

> Status: work in progress (Vultr Agent Rush, Nov 2026).

## Layout
- `orchestrator/` FastAPI control plane (Vultr VM), holds the inference key
- `target_app/`   disposable e-commerce app with a `BUG_PROFILE` flag
- `worker/`       Playwright worker image (runs inside the sandbox)
- `verifier/`     host-side deterministic verifier
- `dashboard/`    React UI
- `scripts/`      infra smoke tests

## TODO (README must cover before submission)
Vultr services used, deployment architecture, agent use case, how it
executes real tasks, containment threat model and tested controls.
