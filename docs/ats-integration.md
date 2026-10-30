# ATS Integration — Design

> Design only (a brief bonus item: "API spec for connecting to an ATS, no need to build"). Builds on the event log in [`architecture.md`](architecture.md).

## Summary

Screening results flow **out** to the client's ATS through signed webhooks fed by a transactional outbox. Hiring outcomes flow **back** through a small REST API. The ATS stays the system of record for hiring; we own the screening data and its audit trail.

## Flows

| Direction | When | How |
| --- | --- | --- |
| Us → ATS | A candidate reaches a final status, is overridden, or books an interview | Webhook from the outbox, retried until acknowledged |
| ATS → us | The ATS hires, rejects, or marks an interview no-show | `POST /api/v1/candidates/{id}/ats-status` |
| ATS → us | Backfill or reconciliation | `GET /api/v1/candidates?updated_since=…` (paginated) |

## Outbox (why not call the ATS directly)

Each turn already writes the new state and its events in one transaction. The outbox adds one more row in that same transaction for the events an ATS cares about (`status_changed`, `override`, `interview_booked`). A dispatcher worker sends pending rows and marks them delivered.

- **No lost updates:** if the ATS is down, rows wait; nothing depends on the ATS being up mid-conversation.
- **At-least-once delivery:** retries with exponential backoff (1 min → 6 h, then alert). Each webhook carries `Idempotency-Key: <event id>` so the ATS can drop duplicates.
- **Ordering:** per candidate, by event sequence; the payload always carries the full current snapshot, so a late older event can't overwrite a newer one (`snapshot_version`).

## Webhook

`POST {ats_webhook_url}` with headers `X-Sazon-Signature: sha256=<HMAC of body with the client's secret>`, `X-Sazon-Timestamp`, `Idempotency-Key`.

```json
{
  "event": "candidate.status_changed",
  "event_id": "7b1c…",
  "occurred_at": "2026-11-03T10:15:00Z",
  "client_id": "grupo_sazon",
  "snapshot_version": 12,
  "candidate": {
    "id": "3f2a…",
    "status": "qualified",
    "location_id": "es-madrid",
    "full_name": "Ana López",
    "language": "es",
    "screening": {
      "license": {"has_license": true, "vehicle": "moto"},
      "location": {"city": "Madrid", "zone": "Chamberí"},
      "availability": ["full_time"],
      "schedule": ["evening"],
      "experience": {"years": 2, "platforms": ["Glovo"]},
      "start_date": "2026-11-09"
    },
    "review_reasons": [],
    "interview_at": "2026-11-04T09:00:00Z",
    "summary": "Qualified: meets every hard requirement. …",
    "consent": {"screening": true, "keep_on_file": null},
    "links": {"dashboard": "https://…/dashboard/candidates/3f2a…"}
  }
}
```

Events: `candidate.status_changed`, `candidate.overridden` (adds `actor`, `reason`), `candidate.interview_booked`.

## REST API (sketch)

Auth: OAuth2 client credentials per client (or a scoped API key), TLS only, rate-limited per client.

```yaml
openapi: 3.1.0
info: {title: Sazón screening — ATS API, version: 1.0.0}
paths:
  /api/v1/candidates:
    get:
      summary: List candidates changed since a time (reconciliation)
      parameters:
        - {name: updated_since, in: query, required: true, schema: {type: string, format: date-time}}
        - {name: status, in: query, schema: {type: string, enum: [qualified, disqualified, needs_review, abandoned]}}
        - {name: cursor, in: query, schema: {type: string}}
      responses:
        "200": {description: Page of candidate snapshots plus next_cursor}
  /api/v1/candidates/{id}:
    get:
      summary: Full candidate snapshot (same shape as the webhook's `candidate`)
  /api/v1/candidates/{id}/ats-status:
    post:
      summary: The ATS reports what happened after screening
      requestBody:
        content:
          application/json:
            schema:
              type: object
              required: [status]
              properties:
                status: {type: string, enum: [interviewed, hired, rejected, no_show, withdrawn]}
                ats_candidate_id: {type: string}
                note: {type: string, maxLength: 500}
      responses:
        "202": {description: Recorded as an event (`ats_status`) on the candidate timeline}
```

## Field mapping (per client config)

ATS products differ, so the mapping lives in the client config, not in code:

| Our field | Typical ATS target |
| --- | --- |
| `full_name` | Candidate first/last name |
| channel contact (WhatsApp number) | Phone |
| `location_id` | Job requisition / location |
| `status` | Application stage (e.g. "Screened – qualified") |
| `screening.*`, `summary` | Custom fields or a structured note |
| `interview_at` | Interview event |

## Data protection

- Send only the screening fields and summary; never transcripts, unless the client explicitly enables it.
- Candidates who declined consent or "keep on file" are not sent beyond their final status.
- Deletions propagate: when retention deletes a record, a `candidate.deleted` webhook asks the ATS to do the same.

## Open questions for the client

Which ATS they use, whether it can receive webhooks or needs polling, and which application stages map to our outcomes.
