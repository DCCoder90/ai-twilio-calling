# twilio-calling

A small, dependency-free Python service that runs live phone
conversations for an AI agent (Muse) placing outbound calls through
Twilio Programmable Voice, under a strict per-call brief. Built for
Ernest Mallett's homelab; this README is the complete manual — with it
and a Twilio account you can deploy the system from scratch anywhere.

## How it works

```
   operator (Muse)                this service               Twilio
        |                               |                        |
        |  POST /briefs (approved       |                        |
        |  brief, ingest secret)        |                        |
        |------------------------------>|  stores briefs/<id>.json
        |                               |                        |
        |  create call via Twilio REST, webhooks -> this service |
        |-------------------------------------------------------->|
        |                               |<-- POST /twilio/voice -|  (call answered)
        |                               |--- opening + <Gather> ->|
        |                               |<-- POST /twilio/gather|  (speech, per turn)
        |                               |--- reply + <Gather> --->|
        |                               |        ...              |
        |                               |<-- POST /twilio/status|  (call ended)
        |  GET /transcripts/<id>        |                        |
        |<------------------------------|  turns + outcome       |
        |  reports outcome to Ernest    |                        |
```

- The service is **stateless except for two directories** on a data
  volume: `briefs/` (one JSON per upcoming call) and `transcripts/`
  (one `.jsonl` per call plus a `.outcome.json` written when it ends).
- The conversation brain is pluggable (see *Conversation brain*). The
  deterministic safety rails are code, not prompt, and run in front of
  any model.
- The operator side (composing briefs, creating calls through Twilio's
  REST API, reading transcripts back) is deliberately *not* in this
  service. In production that is Muse, using two small connectors
  against Twilio's API and this service's operator routes. Anything
  that can POST a brief, create a Twilio call with the right webhook
  URLs, and GET a transcript can operate it.

## The brief

Every call runs from a **brief**: a JSON document the operator writes
and Ernest approves before the call (recipient, purpose, key points —
one approval per call). The brief is the model's entire world: the only
facts it may state, the exact mandate it may act within, and its
deflection/fallback/closing lines. **Briefs never contain personal
details beyond what was approved for that call** — no home address, no
family details, nothing about the kids. That is the system's hard
guardrail, and it is structural: the model cannot leak what it was
never given.

A `call_id` identifies a brief, its call, and its transcript. Use a
fresh `call_id` per call (`^[A-Za-z0-9_-]{1,64}$`); an id that already
has a transcript is spent and the ingest route refuses it with 409.

### Brief fields

| Field | Type | Notes |
|---|---|---|
| `to` | string | Destination number, E.164 (`+1...`). |
| `from_number` | string | Caller ID: a Twilio number on the account. |
| `opening` | string | First thing spoken when the call is answered. |
| `identity` | string | Who the voice is on this call; becomes part of the model's system prompt. |
| `purpose` | string | The job, including the exact mandate: what it may agree to and what it must refuse or escalate. State accepted options as an ONLY list. |
| `facts` | list | `{keywords: [...], answer}` objects. In LLM mode the answers (not the keywords) are rendered into the system prompt as the only specific details the model knows. Keywords serve the deterministic responder. |
| `closing` | string | The line the model says when the call wraps up. |
| `deflect_line` | string | Spoken when a guarded personal question is asked. |
| `fallback_line` | string | Spoken when the model call fails; never dead air. |
| `max_turns` | int ≥ 1 | Hard cap on the party's speech turns, enforced by the server. |
| `listen_prompt` | string, optional | Spoken inside each `<Gather>` while listening. Set to `""` for a silent Gather (recommended in LLM mode — the model's replies end in their own questions, and a spoken "go ahead" after every turn sounds robotic). Default `"Go ahead."`. |

One convention learned in production: **a callback number given out on
a call is the principal's own phone number, never the agent's Twilio
number** — callbacks exist to reach the human, and (until inbound
handling exists) the agent number reaches nobody.

### Example brief

```json
{
  "to": "+15551234567",
  "from_number": "+15557654321",
  "opening": "Hi, this is Muse, an assistant calling on behalf of Ernest. I'd like to book a table for two for Friday, November 13th at 7:00 PM, if you have anything available.",
  "identity": "I'm Muse, an AI assistant calling on behalf of Ernest to arrange a dinner reservation for him.",
  "purpose": "Book a dinner reservation: table for 2 on Friday, November 13 at 7:00 PM, under the name Ernest. You may accept ONLY 7:00 PM, 6:30 PM, or 7:30 PM. If none is available, do not book anything: say you need to check with Ernest, thank the host, and end the call. No prepayment, no packages.",
  "facts": [
    {"keywords": ["name"], "answer": "The reservation is under the name Ernest."},
    {"keywords": ["phone", "callback"], "answer": "The best callback number for Ernest is 202-555-0147."}
  ],
  "closing": "Perfect, thank you so much for your help. We'll see you then. Goodbye.",
  "deflect_line": "I'm not able to share that. I'm only calling to arrange the reservation.",
  "fallback_line": "Sorry, let me stay on the reservation: a table for two, Friday at 7:00 PM, under the name Ernest.",
  "max_turns": 10,
  "listen_prompt": ""
}
```

## Conversation behavior

Per speech turn, in order:

1. The party's speech is appended to the transcript.
2. **Guarded-topic check (code).** Questions seeking personal details
   (address, family, children, finances, medical, etc.) get the brief's
   `deflect_line` and never reach the model. The third guarded question
   in one call ends it (`guarded_wall`).
3. **Stop check (code).** A party asking to stop ends the call
   (`stop_requested`).
4. **The brain answers** (LLM mode) with the brief as its system prompt:
   identity, purpose, approved facts, a fixed role lock (it is the
   *caller*, never the business it phoned), a privacy rule, a
   never-repeat-yourself rule, and a never-agree-outside-the-mandate
   rule. The opening is included as the first assistant turn so the
   model sees itself speaking as the caller from turn one.
5. `max_turns` is enforced by the server regardless of the brain.

End conditions and the `reason` recorded in the outcome file:
`completed` (model appended `[[END]]` after its closing line; the
server strips the marker and hangs up), `guarded_wall`,
`stop_requested`, `max_turns`, `party_hangup` (carrier reported the
call over with no other end condition).

### Conversation brain

Chosen by environment at startup; the startup log line says which is
live (`brain: <model> @ <base URL>` or `brain: deterministic responder`).

- **Deterministic responder** (no `LLM_BASE_URL` set): replies come
  from the brief alone — keyword matching over the prewritten fact
  answers, plus the rails. Fully auditable, limited conversation. This
  was v1; it is also the permanent fallback shape of the rails.
- **LLM mode**: each turn goes to an OpenAI-compatible
  `/chat/completions` endpoint. Point `LLM_BASE_URL` at a
  [LiteLLM](https://docs.litellm.ai/) gateway and the model behind a
  call is whatever `LLM_MODEL` names — local models on Ernest's DGX
  Spark and hosted models behind one knob, swappable with an env
  change and a pod restart. On any model error or timeout the brief's
  `fallback_line` is spoken and the call continues; a failure never
  produces silence, but it also never announces itself, so persistent
  failures look like a script-bound caller — check transcript `tag`
  fields (`llm` vs `llm_fallback`) when grading a call.

Model notes, all learned live in November 2026:

- Through LiteLLM, local Ollama models are named `ollama/<model>`
  (e.g. `ollama/gpt-oss:20b`); the bare name returns 400.
- `claude-sonnet-4-6` is the verified-best phone brain so far
  (~1.3 s/turn, strongest instruction adherence). `claude-haiku-4-5`
  is faster and nearly as good.
- **The Claude 5.x family and Opus 4.7 reject any request carrying a
  `temperature` field (400).** The app sends `temperature: 0.6` by
  default; set `LLM_TEMPERATURE=omit` for those models or every turn
  silently falls back.
- Reasoning models can spend their whole token budget thinking and
  return no speakable text. `max_tokens` is 800 for this reason, any
  `<think>...</think>` in the reply text is stripped before speaking,
  and an empty reply raises the fallback path with the finish reason
  logged.

## Voice modes: Gather vs ConversationRelay

The conversation above runs in one of two transports, chosen by
`VOICE_MODE`:

- **`gather` (default)**: the turn-based TwiML flow in the diagram —
  Twilio transcribes each utterance, POSTs it, waits for TwiML, speaks
  the reply with its stock `<Say>` voice. Simple and fully auditable;
  the cadence is walkie-talkie and the voice cannot be interrupted.
- **`relay`**: the voice webhook returns
  `<Connect><ConversationRelay>` and the call moves to a WebSocket at
  `GET /twilio/relay`. Twilio runs the realtime loop — streaming
  speech-to-text (Deepgram or Google), premium text-to-speech
  (Amazon Polly, Google, or ElevenLabs voices), turn detection, and
  **barge-in** — and this service exchanges only text with it: the
  brief's opening rides as the session's welcome greeting, caller
  speech arrives as `prompt` messages, replies stream back as text
  tokens (the model streams too, sentence by sentence, so speech
  starts on the first sentence), and an `end` message closes the
  session into the `<Hangup/>` that follows the `<Connect>`.

Everything above the transport is identical in both modes: the same
brief, the same deterministic rails in front of the model (guarded
prompts are answered from the brief's lines without a model call), the
same `max_turns` enforcement, the same transcript and outcome files.

Relay adds one behavior Gather cannot have: **heard-tracking**. When
the caller barges in, Twilio reports `utteranceUntilInterrupt` — the
exact text that was spoken before the cut. Generation aborts, and the
transcript's muse turn for that utterance is rewritten to the heard
text (or dropped if nothing was heard), so the model's history next
turn contains only what the caller actually heard. Without this, a
streaming agent references sentences it never finished and repeats
itself; with it, an interruption is just a normal conversational
event.

Costs and limits of relay mode: Twilio charges $0.07/min for
ConversationRelay on top of normal voice minutes (a 3-minute call is
roughly a quarter, all-in). The call's audio is processed by Twilio's
STT/TTS providers — the same disclosure class as a hosted model brain,
one step further (raw audio, not just transcript text). A caller who
says nothing produces no prompt, so a silent call idles until Twilio's
max duration; there is no silence nudge yet. ElevenLabs voices require
the Twilio account to be enabled for ElevenLabs; the Amazon default
needs nothing.

## HTTP routes

| Route | Who calls it | Auth | Purpose |
|---|---|---|---|
| `GET /health` | anyone | none | Liveness; returns `ok`. |
| `POST /twilio/voice?call_id=<id>` | Twilio | Twilio signature | Opening line + first Gather. |
| `POST /twilio/gather?call_id=<id>` | Twilio | Twilio signature | One conversation turn per POST (`SpeechResult` form field). |
| `POST /twilio/status?call_id=<id>` | Twilio | Twilio signature | Status callbacks; logged to the transcript; ends the call record as `party_hangup` when nothing else did. |
| `GET /twilio/relay` | Twilio | Twilio signature on the WebSocket upgrade | ConversationRelay session (only when `VOICE_MODE=relay`); JSON messages per Twilio's relay protocol. |
| `POST /briefs` | operator | `X-Ingest-Secret` | Body `{"call_id": "...", "brief": {...}}`. Validates the brief and stores it atomically. 400 invalid, 401 bad secret, 409 spent call_id, 503 when ingest is not configured. |
| `GET /transcripts/<call_id>` | operator | `X-Ingest-Secret` | `{call_id, turns: [...], outcome: {...}|null}`. 404 when the call is unknown. |

Authentication model:

- **Twilio routes** verify `X-Twilio-Signature`
  (HMAC-SHA1 over the public URL + sorted form fields) whenever
  `TWILIO_AUTH_TOKEN` is set; unsigned or badly signed requests get
  403. The public URL is reconstructed from `X-Forwarded-Proto` /
  `X-Forwarded-Host`, so verification works behind Traefik/Cloudflare.
  With the token unset the server is in local-dev mode: it accepts
  unsigned requests and logs a warning per request. **Never expose a
  deployment without the token set** — and note the failure mode is
  silent: the service runs fine unsigned if the env var is missing.
- **Operator routes** share one secret, `BRIEF_INGEST_SECRET`, compared
  in constant time. Unset means the routes refuse with 503.

## Configuration

### Environment variables

| Variable | Required | Default | Notes |
|---|---|---|---|
| `TWILIO_AUTH_TOKEN` | production | — | Enables signature verification. From a Secret; never in a manifest or git. |
| `BRIEF_INGEST_SECRET` | for operator routes | — | A long random string. The same value lives in the operator's credential store; treat it like a password. |
| `LLM_BASE_URL` | for LLM mode | — | Base of an OpenAI-compatible API, e.g. `http://litellm.ai.svc.cluster.local:4000`. `/chat/completions` is appended. Unset = deterministic responder. |
| `LLM_MODEL` | no | `gpt-oss:20b` | Model name as the gateway knows it (`ollama/...` for local models via LiteLLM). |
| `LLM_API_KEY` | no | — | Bearer key for the gateway (a LiteLLM master key), from a Secret. |
| `LLM_TIMEOUT_SECONDS` | no | `10` | Per-turn model timeout. Twilio will not wait much longer for TwiML. |
| `LLM_TEMPERATURE` | no | `0.6` | A float, or `omit` for models that reject the field (Claude 5.x, Opus 4.7). |
| `VOICE_MODE` | no | `gather` | `gather` = turn-based TwiML (stock `<Say>` voice). `relay` = ConversationRelay streaming (below). |
| `RELAY_TTS_PROVIDER` | no | `Amazon` | Relay TTS provider: `Amazon`, `Google`, or `ElevenLabs` (ElevenLabs needs account enablement on Twilio's side). |
| `RELAY_VOICE` | no | `Polly.Matthew-Neural` | Relay voice, in the provider's naming (`Polly.*` for Amazon, `Google.en-US-Neural2-*` style for Google, a voice ID for ElevenLabs). |
| `RELAY_TRANSCRIPTION_PROVIDER` | no | — (Twilio default) | Relay speech-to-text provider, e.g. `Deepgram` or `Google`. |
| `RELAY_SPEECH_MODEL` | no | — (provider default) | Relay STT model, e.g. a Deepgram model name. |
| `RELAY_INTERRUPTIBLE` | no | `any` | Who may barge in on the relay voice: `any`, `speech`, `dtmf`, or `none`. |

### CLI flags

`--host` (default `127.0.0.1`; the image passes `0.0.0.0` — a bare
loopback bind inside a container or pod is unreachable and was a real
deployment bug), `--port` (default `8765`), `--briefs-dir`,
`--transcripts-dir` (in the image both live under `/data`).

## Deployment

The image is `ghcr.io/dccoder90/ai-twilio-calling:latest`, built by
GitHub Actions on every push to `main` and pullable anonymously.

### docker compose

```sh
mkdir -p data/briefs data/transcripts
docker compose up -d
```

`docker-compose.yml` maps `./data` to `/data` and publishes 8765.

### Kubernetes (the production shape)

- Namespace `ai`; one replica; container port 8765; `/health` readiness
  and liveness probes.
- A persistent volume mounted at `/data` (production uses a hostPath
  folder on the node, with an init container chowning it to the app
  user), containing `briefs/` and `transcripts/`.
- Secrets wired as env vars: `TWILIO_AUTH_TOKEN` (from the Twilio
  account), `LLM_API_KEY` (ExternalSecret from the same store his other
  LiteLLM consumers use), `BRIEF_INGEST_SECRET` (a plain Secret created
  out of band — generate it into a local file, load it into the
  cluster, hand the operator the same value through a secure channel;
  it never appears in git or chat logs).
- After any manifest change, rolling by deleting the pod avoids an
  Argo CD fight over rollout annotations; the image pull policy is
  `Always`, so the replacement pulls the current `:latest`.

### Public HTTPS is your side of the fence

Twilio must reach the service over public HTTPS with a valid
certificate. Production: WAN 443 forwarded to a Traefik public
entrypoint, a proxied Cloudflare DNS record, an Ingress on the public
entrypoint — base URL `https://calls.dcapi.app`. A tunnel product on
the host works too. Two gotchas from production: Cloudflare's default
rules ban the `Python-urllib` user agent (error 1010), so operator
clients must send a normal UA; and the service hostname may not
resolve on the LAN's internal DNS unless a record is added (harmless —
Twilio uses public DNS).

## Placing a call, end to end

1. The principal approves the call: recipient, purpose, key points.
2. The operator writes the brief JSON and ingests it:
   `POST /briefs` with the ingest secret.
3. The operator creates the call through Twilio's REST API:
   `To` = brief's `to`, `From` = brief's `from_number`,
   `Url` = `<base>/twilio/voice?call_id=<id>` (POST),
   `StatusCallback` = `<base>/twilio/status?call_id=<id>` (POST).
4. The call runs itself per *Conversation behavior*.
5. The operator polls Twilio's call status until `completed`, then
   `GET /transcripts/<call_id>` and reports the outcome — what was
   accomplished, per the transcript, not per the plan.

Helper CLIs kept with the project (not in this repo):
`briefs.py create|validate|show` manages local brief files;
`test_local.py` runs the full local test suites (responder, signature
verification, LLM mode against a stub gateway, ingest routes, and the
ConversationRelay WebSocket flow against an SSE stub) —
**currently 72 checks, all passing**, no network or credentials needed.

## Known limitations / roadmap

- **Voice quality**: in `gather` mode replies are spoken with Twilio's
  stock `<Say>` voice, which is robotic enough to garble its delivery
  of spelled or digit-heavy text. `relay` mode (above) is the built
  answer — premium voices, streaming cadence, barge-in. Beyond that,
  the remaining step up is a fully self-hosted audio path: Twilio
  Media Streams with a local STT/TTS stack, which trades Twilio's
  managed loop (and its per-minute fee and provider processing) for a
  real streaming-audio build.
- **Turn-based latency (gather mode)**: Twilio transcribes, the model
  answers, the service speaks. Expect a few seconds per turn. Relay
  mode streams and answers in about half a second at the median
  (Twilio's benchmarks).
- **No voicemail detection yet**: Twilio answering-machine detection
  (`MachineDetection`) is planned; the intent is to hang up on a
  machine verdict rather than leave voicemails.
- **No inbound handling yet**: the Twilio number's voice webhook still
  points at an older setup. Planned: an inbound route that answers
  generically, takes a message, and stores it as a transcript for the
  operator to deliver to the principal. It must never identify the
  principal or the household.
- **Call-end reporting is pull, not push**: the operator learns a call
  ended by checking. A per-call scheduled check loop is planned so
  outcomes are reported without being asked for.
- The outcome summary's `party_turns` counts status callbacks as well
  as speech (cosmetic; the `max_turns` enforcement counts speech only).

## Security notes

- Secrets (`TWILIO_AUTH_TOKEN`, `BRIEF_INGEST_SECRET`, `LLM_API_KEY`)
  live in the deployment's secret store and the operator's credential
  vault, never in this repo, a manifest, or a chat log.
- Transcripts contain the other party's words. They are call records
  for the principal; store and handle them accordingly.
- The brief is the authorization artifact. If a call matters, the
  brief is the record of exactly what was approved for it.
