# twilio-calling

The conversation service for Muse's outbound calls on Ernest's behalf
(Twilio Programmable Voice). Muse's connector creates a call and points
Twilio at this service; the service runs the conversation: opening line,
speech back-and-forth, guardrails, hangup conditions. Full design notes
live with the project, not in this repo.

## Hard guardrail

A call runs from a **brief**: a JSON file naming the recipient, caller ID,
opening, the only facts Muse may state, the purpose, and the closing.
The responder deflects any question asking for personal details,
identifying details, or anything about the kids, and ends the call after
repeated attempts. Nothing personal exists in a brief beyond what Ernest
approved for that specific call.

## Run it (homelab)

```sh
mkdir -p data/briefs data/transcripts
docker compose up -d
```

The image is `ghcr.io/dccoder90/ai-twilio-calling:latest`, built by the
GitHub Actions workflow in this repo on every push to `main`.

The container serves on port 8765:

- `GET /health` — liveness.
- `POST /twilio/voice?call_id=<id>` — Twilio fetches the opening TwiML here.
- `POST /twilio/gather?call_id=<id>` — each speech turn posts here.
- `POST /twilio/status?call_id=<id>` — Twilio status callbacks.

## Public HTTPS is your side of the fence

Twilio's servers must reach the service over public HTTPS. Put your own
front on it: a reverse proxy with a real certificate, Tailscale Funnel on
the host, or a Cloudflare Tunnel running on the host. The resulting base
URL (`https://...`) is what gets handed to the connector when a call is
created. The service itself should not be exposed directly.

## Per-call flow

1. Ernest approves the call (recipient, purpose, key points). One approval
   per call.
2. Muse writes the brief and hands Ernest the brief JSON file.
3. Ernest drops it in `data/briefs/<call_id>.json` on the host.
4. Muse creates the call through Twilio with the webhook base above.
5. Transcripts and the outcome file land in `data/transcripts/` on the
   host; Muse reports the outcome to Ernest from Twilio's call status
   plus the transcript once it's synced back.

Planned v1.1: an authenticated brief-ingest endpoint on the service so
step 3 disappears and Muse pushes the brief directly before dialing.

## Local development

Stdlib-only Python 3.12, no dependencies.

```sh
python3 server.py --port 8765
python3 briefs.py create --spec <spec.json>
python3 test_local.py   # spins a throwaway server and checks the flow
```
