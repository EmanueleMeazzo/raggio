# raggio

## Agent skills

### Issue tracker

Issues are tracked in the repo's GitHub Issues via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default triage vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` (created lazily by `/grill-with-docs`; doesn't exist yet) + `docs/adr/` at the repo root. See `docs/agents/domain.md`.

## Performance work

The current priority is search/retrieval latency, CPU and memory; ingestion is
sufficiently fast for its frequency. Record every performance change in an ADR
or addendum, with scope, compatibility gates, measurement protocol and raw results.
Update `docs/adr/index.md` and record rejected experiments as well as accepted ones.
