# AI interaction log

This file is the record of how Incident Desk was built. The work happened in one Cursor session on 28 October 2026. The model in that session was Grok 4.7. No separate chat product was used. The repository was empty at the start.

## The request

Build an agent, Python or TypeScript, that automates a real platform-engineering task end to end: web UI, backend API, local setup. The bar was autonomy (plan, tools, recovery, a human in the loop), state that survives steps and can be resumed, a UI where an operator can watch and intervene, and an API for the runtime, the tools, and the state. Timebox called out by the prompt: 2–4 hours. A public repo, a README, meaningful git history, and this log were the deliverables.

## Use case chosen

Incident triage, narrowed to the first minutes of a page: gather evidence, drop red herrings, propose one change, wait.

Considered and not built:

- Infra provisioning. Easy to fake with a plan that always applies Terraform, and the human gate is a single "yes" with little recovery.
- Deployment orchestration. Same shape, and it hides the interesting part (what to do) inside a pipeline graph.
- A chatbot over a runbook. That fails the autonomy bar even if the UI looks busy.

Triage was chosen because a wrong conclusion is visible, a tool can fail for an ordinary reason, and the operator's job is to approve an effect rather than to prompt a model.

## Decisions

The platform is a fixture. Reviewers should not need cloud accounts, and the thing being evaluated is the loop. Each incident loads its own snapshot so the charge outage and the full disk do not contaminate each other. Deploy ages are computed from the incident timestamp, not the wall clock. `payments-api` `2.14.3` is always 18 minutes old on `inc-1042`.

The agent does not switch on incident id. The same gap order investigates both pages. Tests check that the charge page queries `edge-gateway` and the disk page does not.

Hypotheses are scored in `reasoning.py` from tool results. The policy chooses the next tool. That split is deliberate: an LLM can be wrong about the next query and still cannot invent the confidence number the operator sees.

The first design injected a failure by querying the alert's metric name after the catalog had already shown that name did not exist. That was removed. An investigator who has the catalog should not call a series it knows is missing. The failures that remain are ones the catalog does not answer:

- The alert window is `10m`. The store keeps `5m`, `15m`, and `1h`. The retry picks `15m` because it is the shortest window that still covers the page.
- The first log query is the alert title. It matches nothing. The next word comes from the snapshot (`timeout` on the charge page, `disk` on the ledger page).

`execute_remediation` is listed in `GET /api/tools` and is not callable from the loop. The approve endpoint is the only caller. A policy that asks to execute is rewritten, and the timeline records the block. A hint such as "expand the volume" creates the next proposal. It does not approve it. Verification re-reads the series that were elevated. A volume expand does not repair charge timeouts, and the run is not marked resolved.

State is one JSON document per run in SQLite. Pause stores the status to return to, including `awaiting_approval`, so a parked approval is still an approval when you come back. Tests open a second `Store` on the same file and continue.

The LLM policy is optional and uses the same `decide()` return shape. A missing key, a transport error, or unparseable JSON falls back to the evidence-gap policy for that step and says so. The default console policy does not call a model, so `make dev` works offline. Asking for `llm` with no key is a 400, not a silent downgrade.

The UI is one page: incident, timeline, plan, hypotheses, and the approve / reject / hint gate. Steps are explicit. "Run until it needs you" stops on a gate. The console rebuilds from `GET /api/runs/{id}`, so a refresh is a resume.

## Correction after the first green run

The charge hypothesis was worded as "the dependency stayed healthy" from the moment the timeout deploy was seen, including steps before the ledger snapshot. The score was already capped until that snapshot existed, but the sentence overclaimed. The claim now says the dependency is unchecked until the snapshot is in, and only then says it stayed healthy. Tests were re-run after that edit.

## What the tests cover

`pytest` — 11 tests:

- `10m` is rejected and `15m` is accepted
- deploy age is 18 minutes for `2.14.3`
- the charge page stops on rollback to `2.14.2`, with the window recovery, the empty log search, the edge lead closed, and the ledger snapshot
- the disk page stops on `expand_volume` and does not query caller metrics when the caller deploy is old
- approval then verification resolves the charge page and the error series return to baseline
- reject, hint "expand the volume", approve, verification fails, both actions end banned, the agent asks for guidance
- a bogus rollback version does not apply and does not leave the gate
- pause plus a new SQLite connection resumes the next step
- a policy that calls `execute_remediation` is rewritten to `get_service`
- fenced JSON from a model parses
- HTTP lists the mutating tool as requiring approval and can take one step

## Git history

The commits are sliced by concern after the loop was running: platform and store, investigator loop, HTTP API and console, tests, then these docs. That is the review order, not a claim that each slice was written in ignorance of the next one.

## How to audit

- `make demo` prints both timelines.
- `tests/test_agent.py` is the behavioral spec.
- `GET /api/tools` shows the mutation boundary.
- `AI_TRANSCRIPT.md` is this file.

## Completeness

This file is the full record of the AI-assisted build. The session was Cursor, the model was Grok 4.7, and the tree was empty when it started. No other chat was used, so there is no second log to attach.

The record covers the whole session:

1. The opening request: an agent for a real platform-engineering task, with a UI, an API, local setup, a plan, tools, recovery, a human gate, and resumable state.
2. The use-case decision: incident triage, after infra provisioning, deployment orchestration, and a runbook chatbot were considered and rejected.
3. The decisions the model was held to: a local platform with a frozen clock, one investigation order for both incidents, hypothesis scores owned by the runtime, recovery from the `10m` window and the empty log search, and `execute_remediation` reachable only through approval.
4. The correction after the first green run: the charge claim no longer says the dependency stayed healthy before the ledger snapshot exists.
5. The close of the session: the eleven tests listed above, then this log.

AI use on the build is this session. The running desk does not need a model key. The evidence-gap policy is the default, and the optional LLM policy uses the same `decide()` shape when a key is present.
