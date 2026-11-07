# Submission text and video script (draft; edit freely)

## Fields

**Title:** PolicyReplay: Workflow Red Team

**Short description** (one line):
Turns written business policies into adversarial browser tests, runs them in a hardened Vultr sandbox, and proves violations with deterministic code.

**Live app URL:** https://139-84-216-65.sslip.io/
**Public repository:** https://github.com/kritantdev/policyreplay  (MIT)
**Demo platform:** Vultr (Cloud Compute VM + Serverless Inference)

**Tags:** ai-agents, sandbox, containment, vultr, serverless-inference, playwright, docker, security-testing, workflow-testing, fastapi

## Long description

**The problem.** Companies write operating policies, and their web apps are supposed to enforce them. But normal QA checks
whether a feature *works*, not whether someone can *bypass* it: a support agent who issues a refund with no manager
approval, a customer who opens another customer's order, a staff member who promotes themselves to administrator. These
failures cost money and expose data, and they hide behind an application that looks perfectly functional.

**What PolicyReplay does.** It reads a written policy and turns it into adversarial browser tests. A Vultr-hosted model
extracts structured rules and proposes attack scenarios. Code then checks everything the model says: each rule's source
clause must appear verbatim in the policy, thresholds must appear in the clause, and each browser plan may only use a
fixed action vocabulary. A human approves the scenarios. Then an agent drives a real browser against a disposable
e-commerce app, once on a deliberately buggy build and once on a correct build, and a verifier decides what happened.

**The key design rule: the LLM interprets and plans; code proves.** The verdict (VIOLATION, PASS or INCONCLUSIVE) comes from
deterministic Python reading pre-run and post-run database snapshots together with the HTTP status the browser actually
saw. The model supplies no query and no verifier parameter, so it cannot create a finding. If the evidence is missing or
conflicts, the answer is INCONCLUSIVE, never a guess. The correct build is a built-in false-positive control.

**Blast Radius Zero.** Every run executes inside throwaway containers on an internal Docker network with no internet egress:
read-only root filesystem, all Linux capabilities dropped, no-new-privileges, a non-root user, and limits on processes, memory
and CPU. No API key and no Docker socket ever enters a sandbox, and the verifier runs on the host, outside it. When a run ends,
both containers and the network are destroyed and the before/after inventory is recorded. We tested the claims: root writes
fail, the internet and the cloud metadata address are unreachable, a memory hog is killed (exit 137), and a fork loop is capped
by the process limit.

**Vultr stack.** A Vultr Cloud Compute VM runs the FastAPI orchestrator, the Docker sandbox and the public app behind HTTPS.
Vultr Serverless Inference (OpenAI-compatible endpoint) powers rule extraction and scenario generation. We benchmarked five
hosted models on our own validation and picked the one that passed every check fastest.

**Results.** Three policies are tested end to end: refunds above INR 10,000 need manager approval, only administrators can change
roles, and customers cannot read other customers' orders. On the Vultr VM, five consecutive full runs through the public
dashboard each detected all three planted violations with zero false positives, zero inconclusive verdicts and confirmed
cleanup.

**Who it is for.** Engineering, QA and security teams that own business-critical web apps and need repeatable proof, tied to
the original policy clause, that controls are actually enforced in software.

**Honest limits.** This is a workflow tester for e-commerce policies, not a compliance certification, and it does not claim to
work on arbitrary web apps. Docker is not a complete boundary against kernel-level exploits: the threat model is an untrusted
app and browser, not a kernel-exploit attacker. Chromium's own sandbox is off because it cannot run in a capability-free
container, so the container is the boundary. Some evidence rows are written by the app under test, which is why every verdict
also needs the browser-observed HTTP status to agree. Two of our five sample policies are listed as "not tested" because they
have no verifier yet; the product says so instead of faking a result.

## Video script (about 90 seconds; record on the public URL)

| Time | Show | Say |
|---|---|---|
| 0:00 | Dashboard, policy text on screen | "This policy says refunds above ten thousand rupees need manager approval. The app looks fine. Does it actually enforce it?" |
| 0:08 | Click **Extract rules**; rules and clauses appear; point at the "live model output" badge and the "not tested" list | "A model on Vultr Serverless Inference turns the policy into rules, and code checks every one against the original text. Policies we can't verify are listed as not tested, not faked." |
| 0:22 | Scenarios table; click **Approve** | "It proposes attacks, and nothing runs until a human approves." |
| 0:30 | **Run**; live screen scrolling | "Each attack runs in a throwaway sandbox, once on a buggy build and once on a correct build." |
| 0:48 | Evidence report; the red banner; open the P-01 buggy card: actions, screenshot "Refund of 15000 issued", observed database state | "The policy said no. The application said yes. A support user refunded fifteen thousand rupees with no approval. The verdict comes from the database and the HTTP status, not from the model." |
| 1:02 | Same scenario, correct build: PASS (HTTP 403, no refund row) | "The correct build blocks it. That's our false-positive control." |
| 1:08 | **Containment moment:** terminal with `probe` (root write blocked, no route to the internet or metadata address), `stress` (exit 137, fork capped) and an empty `docker ps -a` | "Blast radius zero. Read-only root, no capabilities, no network route, hard limits. A memory hog is killed, and after every run nothing is left behind." |
| 1:22 | Summary tiles: 3 planted violations, 0 false positives, cleanup confirmed; then the URL | "Five clean runs in a row on Vultr. The LLM interprets and plans. Code proves." |

**Recording tips.** The live model call takes 10 to 30 seconds. Either cut the wait or keep it and add a "sped up" caption, but
never pass replayed output off as live (the badge shows which it is). Record the terminal from the VM over SSH with a large
font. Do one practice take first, then a second take for the final.

**Say:** "tested", "in our sandbox", "three policies", "planted violations", "we verified".
**Do not say:** "works on any web app", "unbreakable" or "secure against all escapes", "nothing like this exists",
"compliance certified".

## Slide outline (7 slides)

1. **Title:** PolicyReplay, Workflow Red Team; one-line promise; live URL.
2. **Problem:** policies vs software behaviour; three real-world failure examples.
3. **Solution flow:** policy, rules, approved attacks, sandboxed browser, verdict, evidence report.
4. **Architecture:** `docs/architecture.svg`; point at what runs on Vultr.
5. **Containment:** flags and limits table; the probe and stress results; cleanup inventory.
6. **Results:** 5 clean runs, 3 of 3 planted violations, 0 false positives; screenshot of the evidence report.
7. **Business value, limits, next steps:** QA and security teams; honest limits; next: more policies, gVisor, vision review.
