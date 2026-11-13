#!/usr/bin/env python3
"""Webhook server for the Twilio outbound calling system.

Python 3.12 standard library only. Serves the TwiML endpoints Twilio
calls during a live call:

  POST /twilio/voice?call_id=<id>   opening <Say> + speech <Gather>
  POST /twilio/gather?call_id=<id>  one back-and-forth turn
  POST /twilio/status               call status callbacks (logged)
  POST /briefs                      ingest one approved brief (JSON)
  GET  /transcripts/<call_id>       read a call's transcript + outcome

Voice modes: the default "gather" mode above is turn-based TwiML. With
VOICE_MODE=relay the voice webhook instead hands the call to Twilio
ConversationRelay (<Connect><ConversationRelay>) and the conversation
runs over a WebSocket at GET /twilio/relay: Twilio does streaming
speech-to-text and premium text-to-speech with native barge-in, this
server exchanges text with it, and the same briefs, deterministic
rails, transcripts, and outcomes apply. See RelaySession below and the
README's ConversationRelay section.

The /briefs and /transcripts routes are how Muse operates the system
hands-free: Ernest approves a call in chat, Muse posts the brief, dials,
and later fetches the transcript to report the outcome. Both routes
require the BRIEF_INGEST_SECRET environment variable, sent as the
X-Ingest-Secret header; when it is unset both routes refuse with 503.

Run locally with:
  python3 server.py --port 8765        (binds 127.0.0.1 by default)
  python3 server.py --host 0.0.0.0     (container/pod: accept non-loopback)

Twilio needs a public HTTPS base URL in front of this server before any
real call. All conversation behavior is deterministic (see responder.py)
and driven by the per-call brief in briefs/<call_id>.json (see briefs.py).

Request authentication: when the TWILIO_AUTH_TOKEN environment variable
is set, every POST to /twilio/* must carry a valid X-Twilio-Signature
header or it is rejected with 403. When the variable is unset (local
development), requests are accepted and a warning is logged per request;
never run a public deployment without it.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefs as brief_store
import llm
import responder
import transcripts


def twiml_document(body_inner):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{body_inner}</Response>'


def say(text):
    return f"<Say>{escape(text)}</Say>"


def gather(call_id, listen_prompt):
    action = escape(f"/twilio/gather?call_id={call_id}", {'"': "&quot;"})
    # An empty listen_prompt means a bare Gather: just listen. In LLM
    # mode the model's replies end in their own questions, and tacking
    # a spoken "Go ahead." onto every turn sounds robotic and confuses
    # the other party (observed live Nov 12, 2026).
    inner = say(listen_prompt) if listen_prompt else ""
    return (
        f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto">'
        f"{inner}</Gather>"
    )


def continue_twiml(call_id, brief, parts):
    body = "".join(say(p) for p in parts) + gather(call_id, brief["listen_prompt"])
    return twiml_document(body)


# --- inbound message line ----------------------------------------------
# The agent number takes messages from whoever dials it. The greeting
# identifies nothing (no names, no household), one speech Gather
# records the message, and the transcript lands like any other call's
# so the delivery sweep can find it. Guardrails mirror outbound:
# reveal nothing, confirm nothing about whose number it is.
INBOUND_GREETING = (
    "Hello. You've reached an automated message service. No one is "
    "available to take your call, but you can leave a message and it "
    "will be passed along. Please speak your message now."
)
INBOUND_THANKS = (
    "Thank you. Your message has been recorded and will be passed "
    "along. Goodbye."
)
INBOUND_NO_MESSAGE = (
    "I did not catch a message, so I will let you go. Goodbye."
)


def say_voiced(text):
    """A <Say> in the relay voice (Say-format name) for the inbound
    greeting lines, so the message line does not sound robotic."""
    return f'<Say voice="Polly.Joanna-Generative">{escape(text)}</Say>'


def inbound_call_id(form):
    sid = (form.get("CallSid") or "unknown").strip()
    return ("inbound-" + sid)[:64]


def end_twiml(parts):
    return twiml_document("".join(say(p) for p in parts) + "<Hangup/>")


def twilio_signature(auth_token, url, form):
    """Twilio's request signature: base64(HMAC-SHA1(token, url + sorted
    name/value pairs of the POST body concatenated without separators))."""
    data = url + "".join(f"{name}{form[name]}" for name in sorted(form))
    mac = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


# --- WebSocket plumbing (ConversationRelay) ---------------------------
# Hand-rolled RFC 6455, just enough for ConversationRelay's JSON text
# messages: the project rule is standard library only, and a relay
# session is one text message at a time in each direction.

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_accept_key(key):
    digest = hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def ws_encode_frame(payload, opcode=0x1):
    data = payload.encode("utf-8") if isinstance(payload, str) else payload
    head = bytearray([0x80 | opcode])
    n = len(data)
    if n < 126:
        head.append(n)
    elif n <= 0xFFFF:
        head.append(126)
        head += struct.pack(">H", n)
    else:
        head.append(127)
        head += struct.pack(">Q", n)
    return bytes(head) + data


def ws_read_frame(rfile):
    """Read one WebSocket frame; returns (fin, opcode, payload bytes).
    Raises ConnectionError when the stream ends or is malformed."""
    raw = rfile.read(2)
    if len(raw) < 2:
        raise ConnectionError("websocket closed")
    fin = bool(raw[0] & 0x80)
    opcode = raw[0] & 0x0F
    masked = bool(raw[1] & 0x80)
    length = raw[1] & 0x7F
    if length == 126:
        ext = rfile.read(2)
        if len(ext) < 2:
            raise ConnectionError("websocket closed")
        length = struct.unpack(">H", ext)[0]
    elif length == 127:
        ext = rfile.read(8)
        if len(ext) < 8:
            raise ConnectionError("websocket closed")
        length = struct.unpack(">Q", ext)[0]
    mask = rfile.read(4) if masked else b""
    if masked and len(mask) < 4:
        raise ConnectionError("websocket closed")
    payload = b""
    while len(payload) < length:
        chunk = rfile.read(length - len(payload))
        if not chunk:
            raise ConnectionError("websocket closed mid-frame")
        payload += chunk
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return fin, opcode, payload


def relay_twiml(handler, parsed, call_id, brief, relay):
    """TwiML handing the call to ConversationRelay. The brief's opening
    becomes the welcome greeting (Twilio speaks it the moment the
    session connects, before any WebSocket round trip), the call_id
    rides along as a <Parameter> and arrives in the setup message's
    customParameters, and a <Hangup/> follows the <Connect> so the call
    ends when the relay session does."""

    def attr(value):
        return escape(str(value), {'"': "&quot;"})

    public = urlparse(handler._public_url(parsed))
    scheme = "wss" if public.scheme == "https" else "ws"
    ws_url = f"{scheme}://{public.netloc}/twilio/relay"
    # ConversationRelay takes the BARE voice name (e.g. "Matthew-Neural"
    # for Amazon), unlike <Say>'s "Polly.Matthew-Neural". The prefixed
    # form is accepted at session setup but every synthesis then fails
    # and the caller hears silence (verified live Nov 12, 2026), so
    # normalize the common mistake away.
    voice = str(relay["voice"])
    if relay["tts_provider"] == "Amazon" and voice.startswith("Polly."):
        voice = voice[len("Polly."):]
    attrs = [
        f'url="{attr(ws_url)}"',
        f'ttsProvider="{attr(relay["tts_provider"])}"',
        f'voice="{attr(voice)}"',
        f'interruptible="{attr(relay["interruptible"])}"',
        f'interruptSensitivity="{attr(relay.get("interrupt_sensitivity") or "medium")}"',
        'dtmfDetection="true"',
        f'welcomeGreeting="{attr(brief["opening"])}"',
        'welcomeGreetingInterruptible="any"',
    ]
    if relay.get("transcription_provider"):
        attrs.append(f'transcriptionProvider="{attr(relay["transcription_provider"])}"')
    if relay.get("speech_model"):
        attrs.append(f'speechModel="{attr(relay["speech_model"])}"')
    body = (
        f"<Connect><ConversationRelay {' '.join(attrs)}>"
        f'<Parameter name="call_id" value="{attr(call_id)}"/>'
        "</ConversationRelay></Connect><Hangup/>"
    )
    return twiml_document(body)


class CallHandler(BaseHTTPRequestHandler):
    server_version = "TwilioCalling/0.1"

    # --- plumbing -----------------------------------------------------
    def log_message(self, fmt, *args):  # keep stdout clean; transcripts are the record
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, content_type="text/xml; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def _call_id(self, parsed):
        params = parse_qs(parsed.query)
        values = params.get("call_id")
        return values[0] if values else None

    def _public_url(self, parsed):
        """The URL Twilio requested, reconstructed behind proxies: Twilio
        signs the public URL, so prefer X-Forwarded-Proto / -Host when a
        reverse proxy or ingress supplies them."""
        proto = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",")[0].strip()
        url = f"{proto}://{host}{parsed.path}"
        if parsed.query:
            url += f"?{parsed.query}"
        return url

    def _verify_twilio(self, parsed, form):
        token = getattr(self.server, "auth_token", None)
        if not token:
            sys.stderr.write(
                "warning: TWILIO_AUTH_TOKEN unset; accepting unsigned "
                f"request to {parsed.path}\n"
            )
            return True
        provided = self.headers.get("X-Twilio-Signature") or ""
        expected = twilio_signature(token, self._public_url(parsed), form)
        return hmac.compare_digest(provided, expected)

    def _load_brief_or_error(self, call_id):
        if not call_id:
            return None, end_twiml("Sorry, this call is not configured. Goodbye.")
        try:
            return brief_store.load_brief(call_id, self.server.briefs_dir), None
        except brief_store.BriefError:
            return None, end_twiml("Sorry, this call is not configured. Goodbye.")

    # --- ingest / transcript access ------------------------------------
    def _check_ingest_auth(self):
        """True when the request carries the ingest secret. Returns
        False after sending 503 (feature disabled) or 401 (bad secret)."""
        secret = getattr(self.server, "ingest_secret", None)
        if not secret:
            self._send(503, "ingest disabled: BRIEF_INGEST_SECRET is not set",
                       "text/plain; charset=utf-8")
            return False
        provided = self.headers.get("X-Ingest-Secret") or ""
        if not hmac.compare_digest(provided, secret):
            self._send(401, "bad or missing X-Ingest-Secret", "text/plain; charset=utf-8")
            return False
        return True

    def _json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return json.loads(raw)

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _handle_ingest(self):
        if not self._check_ingest_auth():
            return
        try:
            payload = self._json_body()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            self._send_json(400, {"error": f"request body is not valid JSON: {exc}"})
            return
        call_id = payload.get("call_id") if isinstance(payload, dict) else None
        brief = payload.get("brief") if isinstance(payload, dict) else None
        if not call_id or not brief_store.CALL_ID_RE.match(str(call_id)):
            self._send_json(400, {"error": "call_id is required and must match [A-Za-z0-9_-]{1,64}"})
            return
        if not isinstance(brief, dict):
            self._send_json(400, {"error": "brief is required and must be a JSON object"})
            return
        # A call_id whose call already happened is spent; its transcript
        # is the record and a new brief must not resurrect the id.
        tpath = transcripts.transcript_path(call_id, self.server.transcripts_dir)
        if tpath.exists():
            self._send_json(409, {"error": f"call_id {call_id!r} already has a transcript; use a new call_id"})
            return
        try:
            brief_store.save_brief(call_id, brief, self.server.briefs_dir)
        except brief_store.BriefError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        self._send_json(200, {"call_id": call_id, "stored": True})

    def _handle_transcript_index(self):
        """GET /transcripts — the call_id index (ingest auth). The
        inbound delivery sweep enumerates new message transcripts
        from this; per-call records stay on the fetch route."""
        if not self._check_ingest_auth():
            return
        tdir = self.server.transcripts_dir
        ids = sorted(p.stem for p in tdir.glob("*.jsonl")) if tdir.exists() else []
        self._send_json(200, {"call_ids": ids})

    def _handle_transcript_fetch(self, parsed):
        if not self._check_ingest_auth():
            return
        call_id = parsed.path[len("/transcripts/"):]
        if not brief_store.CALL_ID_RE.match(call_id or ""):
            self._send_json(400, {"error": "invalid call_id"})
            return
        turns = transcripts.read_turns(call_id, self.server.transcripts_dir)
        outcome = None
        opath = transcripts.outcome_path(call_id, self.server.transcripts_dir)
        if opath.exists():
            try:
                outcome = json.loads(opath.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                outcome = None
        if not turns and outcome is None:
            self._send_json(404, {"error": f"no transcript for call_id {call_id!r}"})
            return
        self._send_json(200, {"call_id": call_id, "turns": turns, "outcome": outcome})

    # --- routes -------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, "ok", "text/plain; charset=utf-8")
            return
        if parsed.path == "/twilio/relay":
            self._handle_relay(parsed)
            return
        if parsed.path == "/twilio/voice":
            self._handle_voice(parsed, None)
            return
        if parsed.path == "/transcripts":
            self._handle_transcript_index()
            return
        if parsed.path.startswith("/transcripts/"):
            self._handle_transcript_fetch(parsed)
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/briefs":
            self._handle_ingest()
            return
        if parsed.path not in ("/twilio/voice", "/twilio/gather", "/twilio/status",
                               "/twilio/inbound", "/twilio/inbound-message"):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        form = self._form()
        if not self._verify_twilio(parsed, form):
            self._send(403, "invalid Twilio signature", "text/plain; charset=utf-8")
            return
        if parsed.path == "/twilio/voice":
            self._handle_voice(parsed, form)
        elif parsed.path == "/twilio/gather":
            self._handle_gather(parsed, form)
        elif parsed.path == "/twilio/inbound":
            self._handle_inbound(parsed, form)
        elif parsed.path == "/twilio/inbound-message":
            self._handle_inbound_message(parsed, form)
        else:
            self._handle_status(parsed, form)

    # --- handlers -----------------------------------------------------
    def _handle_voice(self, parsed, form=None):
        call_id = self._call_id(parsed)
        brief, error = self._load_brief_or_error(call_id)
        if brief is None:
            self._send(200, error)
            return
        # Voicemail detection (answering-machine detection runs on the
        # Twilio side; with synchronous AMD the verdict arrives as
        # AnsweredBy in this webhook and nothing has been spoken yet).
        # Ernest's standing rule is that calls never leave voicemail,
        # so a machine or fax verdict hangs up before the opening.
        answered_by = ((form or {}).get("AnsweredBy") or "").strip().lower()
        if answered_by.startswith("machine") or answered_by == "fax":
            transcripts.append_turn(
                call_id, "party",
                f"answered_by: {answered_by} — hung up without speaking "
                "(voicemail detection)",
                "status", transcripts_dir=self.server.transcripts_dir,
            )
            transcripts.write_outcome(
                call_id, brief, "voicemail_detected",
                transcripts_dir=self.server.transcripts_dir,
            )
            self._send(200, twiml_document("<Hangup/>"))
            return
        transcripts.append_turn(
            call_id, "muse", brief["opening"], "opening",
            transcripts_dir=self.server.transcripts_dir,
        )
        relay = getattr(self.server, "relay", None)
        if relay is not None:
            self._send(200, relay_twiml(self, parsed, call_id, brief, relay))
            return
        body = say(brief["opening"]) + gather(call_id, brief["listen_prompt"])
        self._send(200, twiml_document(body))

    def _handle_inbound(self, parsed, form):
        """Answer a call to the agent number: generic greeting that
        identifies nothing, then one speech Gather for the message.
        The TwiML after </Gather> covers the caller who says nothing
        at all (Gather falls through on empty input)."""
        call_id = inbound_call_id(form)
        caller = (form.get("From") or "").strip()
        tdir = self.server.transcripts_dir
        transcripts.append_turn(
            call_id, "party",
            f"inbound call from {caller or 'an unknown number'}",
            "status", transcripts_dir=tdir,
        )
        transcripts.append_turn(
            call_id, "muse", INBOUND_GREETING, "opening",
            transcripts_dir=tdir,
        )
        action = escape(f"/twilio/inbound-message?call_id={call_id}",
                        {'"': "&quot;"})
        body = (
            say_voiced(INBOUND_GREETING)
            + f'<Gather input="speech" action="{action}" method="POST" '
              'speechTimeout="auto" timeout="10"></Gather>'
            + say_voiced(INBOUND_NO_MESSAGE)
            + "<Hangup/>"
        )
        self._send(200, twiml_document(body))

    def _handle_inbound_message(self, parsed, form):
        """The inbound Gather's action: record the message, thank the
        caller, hang up. An empty result still closes the record so
        the delivery sweep can report a message-less call."""
        call_id = self._call_id(parsed)
        tdir = self.server.transcripts_dir
        speech = (form.get("SpeechResult") or "").strip()
        record = {"to": (form.get("From") or "").strip(),
                  "purpose": "Inbound message left on the agent number"}
        if speech:
            transcripts.append_turn(
                call_id, "party", speech, "speech", transcripts_dir=tdir)
            transcripts.write_outcome(
                call_id, record, "inbound_message", transcripts_dir=tdir)
            self._send(200, twiml_document(
                say_voiced(INBOUND_THANKS) + "<Hangup/>"))
        else:
            transcripts.write_outcome(
                call_id, record, "inbound_no_message", transcripts_dir=tdir)
            self._send(200, twiml_document(
                say_voiced(INBOUND_NO_MESSAGE) + "<Hangup/>"))

    def _handle_relay(self, parsed):
        """ConversationRelay WebSocket endpoint. Validates the upgrade
        (including Twilio's signature over the wss URL when an auth
        token is configured), answers 101, then runs the session on
        this handler thread."""
        if getattr(self.server, "relay", None) is None:
            self._send(404, "relay mode is not enabled", "text/plain; charset=utf-8")
            return
        key = self.headers.get("Sec-WebSocket-Key")
        if (self.headers.get("Upgrade") or "").lower() != "websocket" or not key:
            self._send(400, "expected a websocket upgrade", "text/plain; charset=utf-8")
            return
        token = getattr(self.server, "auth_token", None)
        if token:
            host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
                    or "").split(",")[0].strip()
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            provided = self.headers.get("X-Twilio-Signature") or ""
            candidates = (f"wss://{host}{path}", f"https://{host}{path}")
            if not any(hmac.compare_digest(provided, twilio_signature(token, url, {}))
                       for url in candidates):
                self._send(403, "invalid Twilio signature", "text/plain; charset=utf-8")
                return
        else:
            sys.stderr.write(
                "warning: TWILIO_AUTH_TOKEN unset; accepting unsigned "
                "websocket relay connection\n"
            )
        accept = ws_accept_key(key)
        self.close_connection = True
        self.connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii")
        )
        self.connection.settimeout(1200)
        RelaySession(self, parsed).run()

    def _handle_gather(self, parsed, form):
        call_id = self._call_id(parsed)
        brief, error = self._load_brief_or_error(call_id)
        if brief is None:
            self._send(200, error)
            return
        speech = form.get("SpeechResult", "") or ""
        tdir = self.server.transcripts_dir

        turn_number = transcripts.party_turn_count(call_id, tdir) + 1
        guarded_so_far = transcripts.deflect_count(call_id, tdir)
        transcripts.append_turn(call_id, "party", speech, "speech", transcripts_dir=tdir)

        reply = self._decide(brief, speech, turn_number, guarded_so_far, call_id, tdir)
        for part in reply.parts:
            transcripts.append_turn(
                call_id, "muse", part, "reply", tag=reply.tag, transcripts_dir=tdir
            )

        if reply.end:
            transcripts.write_outcome(call_id, brief, reply.reason, transcripts_dir=tdir)
            self._send(200, end_twiml(reply.parts))
        else:
            self._send(200, continue_twiml(call_id, brief, reply.parts))

    def _decide(self, brief, speech, turn_number, guarded_so_far, call_id, tdir):
        """Choose the reply for one party utterance.

        Without an LLM brain configured, the deterministic responder
        decides everything. With one, the deterministic rails still run
        first (guarded topics deflect by rule and never reach the model;
        stop requests end by rule), then the model converses, and any
        model failure falls back to the brief's scripted fallback line.
        """
        brain = getattr(self.server, "brain", None)
        if brain is None:
            return responder.respond(brief, speech, turn_number, guarded_so_far)

        deflect = brief["deflect_line"]
        closing = brief["closing"]
        if responder.is_guarded(speech):
            if guarded_so_far + 1 >= responder.GUARDED_LIMIT:
                return responder.Reply([deflect, closing], end=True,
                                       reason="guarded_wall", tag="guarded_end")
            return responder.Reply([deflect], tag="deflect")
        if responder.is_stop_request(speech):
            return responder.Reply([closing], end=True,
                                   reason="stop_requested", tag="stop")

        turns = transcripts.read_turns(call_id, transcripts_dir=tdir)
        try:
            text, end = brain.reply(brief, turns)
            reply = responder.Reply([text], end=end,
                                    reason="completed" if end else None, tag="llm")
        except llm.LLMError as exc:
            sys.stderr.write(f"llm brain failed for {call_id}: {exc}\n")
            reply = responder.Reply([brief["fallback_line"]], tag="llm_fallback")
        if not reply.end and turn_number >= int(brief["max_turns"]):
            reply = responder.Reply(reply.parts + [closing], end=True,
                                    reason="max_turns", tag=reply.tag)
        return reply

    def _handle_status(self, parsed, form):
        call_id = self._call_id(parsed)
        call_sid = form.get("CallSid", "")
        status = form.get("CallStatus", "")
        # call_id names files on disk via the transcript store; never let
        # an unchecked value reach a path. Brief ids are validated the
        # same way in briefs.py.
        if call_id and not brief_store.CALL_ID_RE.match(call_id):
            self._send(400, "invalid call_id", "text/plain; charset=utf-8")
            return
        log_id = call_id or call_sid or "unknown"
        if not brief_store.CALL_ID_RE.match(log_id):
            log_id = "unknown"
        transcripts.append_turn(
            log_id, "party",
            f"status callback: CallSid={call_sid} CallStatus={status} "
            f"From={form.get('From', '')} To={form.get('To', '')} "
            f"Duration={form.get('CallDuration', '')}",
            "status", transcripts_dir=self.server.transcripts_dir,
        )
        # If the carrier reports the call over and no responder end
        # condition recorded an outcome, the party hung up mid-call.
        if call_id and status in ("completed", "busy", "failed", "no-answer", "canceled"):
            from transcripts import outcome_path

            if not outcome_path(call_id, self.server.transcripts_dir).exists():
                try:
                    brief = brief_store.load_brief(call_id, self.server.briefs_dir)
                except brief_store.BriefError:
                    brief = {}
                transcripts.write_outcome(
                    call_id, brief, "party_hangup", transcripts_dir=self.server.transcripts_dir
                )
        self._send(200, "", "text/plain; charset=utf-8")


class RelaySession:
    """One ConversationRelay WebSocket session.

    The reader loop runs on the connection's handler thread. Model
    generation runs on a worker thread so an interrupt message can be
    read and acted on while text is still streaming out; the worker
    checks the cancel flag between sentence chunks. Sends are
    serialized with a lock. Transcript writes for a reply happen either
    on the worker (normal completion) or on the reader thread
    (interrupt fix-up, after the worker has stopped), never both at
    once; the one soft spot is a status-callback append landing during
    an interrupt rewrite, which can lose that single status line.

    Heard-tracking: Twilio's interrupt message carries
    utteranceUntilInterrupt, the exact text the party heard. The
    transcript's muse turn for the interrupted utterance is rewritten
    to it (or dropped when nothing was heard), so the history the
    model sees next turn contains only what was actually said.
    """

    def __init__(self, handler, parsed):
        self.server = handler.server
        self.conn = handler.connection
        self.rfile = handler.rfile
        self.query = parse_qs(parsed.query)
        self.send_lock = threading.Lock()
        self.call_id = None
        self.brief = None
        self.brain = getattr(self.server, "brain", None)
        self.tdir = self.server.transcripts_dir
        self.over = False
        self.outcome_written = False
        self.end_thread = None
        self.gen_thread = None
        self.gen_done = threading.Event()
        self.gen_done.set()
        self.cancel = threading.Event()
        # The opening is already in the transcript (the voice webhook
        # appended it) and Twilio speaks it as the welcome greeting.
        self.utterance_appended = True
        self.partial_prompt = ""

    # --- transport ------------------------------------------------------
    def _send_obj(self, obj):
        frame = ws_encode_frame(json.dumps(obj))
        with self.send_lock:
            self.conn.sendall(frame)

    def _send_text(self, token, last):
        self._send_obj({"type": "text", "token": token, "last": last})

    def _close_socket(self):
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def _read_message(self):
        """Assemble one complete text message; None on close."""
        chunks = []
        while True:
            fin, opcode, payload = ws_read_frame(self.rfile)
            if opcode == 0x8:
                return None
            if opcode == 0x9:  # ping -> pong
                with self.send_lock:
                    self.conn.sendall(ws_encode_frame(payload, opcode=0xA))
                continue
            if opcode == 0xA:
                continue
            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", "replace")

    def run(self):
        try:
            while not self.over:
                message = self._read_message()
                if message is None:
                    break
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                etype = event.get("type")
                if etype == "setup":
                    self._on_setup(event)
                elif etype == "prompt":
                    self._on_prompt(event)
                elif etype == "interrupt":
                    self._on_interrupt(event)
                elif etype == "dtmf":
                    if self.call_id:
                        transcripts.append_turn(
                            self.call_id, "party", f"dtmf: {event.get('digit', '')}",
                            "dtmf", transcripts_dir=self.tdir)
                elif etype == "error":
                    sys.stderr.write(
                        f"conversationrelay error for {self.call_id}: "
                        f"{event.get('description')}\n")
        except (ConnectionError, OSError):
            pass
        finally:
            self.over = True
            if self.end_thread is not None:
                # A finish is mid-flight: the end message goes out on a
                # speaking-time delay so it cannot truncate the closing
                # line. Wait for it rather than racing it with a close
                # frame (which Twilio would read as a dropped session).
                self.end_thread.join(timeout=30)
            try:
                with self.send_lock:
                    self.conn.sendall(ws_encode_frame(b"", opcode=0x8))
            except OSError:
                pass

    # --- events ---------------------------------------------------------
    def _on_setup(self, event):
        params = event.get("customParameters") or {}
        call_id = params.get("call_id") or (self.query.get("call_id") or [None])[0]
        if not call_id or not brief_store.CALL_ID_RE.match(str(call_id)):
            self._send_obj({"type": "end"})
            self.over = True
            return
        self.call_id = str(call_id)
        try:
            self.brief = brief_store.load_brief(self.call_id, self.server.briefs_dir)
        except brief_store.BriefError:
            self._send_obj({"type": "end"})
            self.over = True

    def _on_prompt(self, event):
        if self.brief is None or self.over:
            return
        text = event.get("voicePrompt") or ""
        if event.get("last") is False:
            self.partial_prompt += text
            return
        speech = (self.partial_prompt + text).strip()
        self.partial_prompt = ""
        # A prompt normally arrives only after the previous reply cycle
        # finished or was interrupted (which already joined the
        # worker). Join defensively so transcript writes never race.
        self._join_generation(timeout=5)
        if self.over:
            return
        transcripts.append_turn(self.call_id, "party", speech, "speech",
                                transcripts_dir=self.tdir)
        turn_number = transcripts.party_turn_count(self.call_id, self.tdir)
        guarded_so_far = transcripts.deflect_count(self.call_id, self.tdir)
        brief = self.brief

        if self.brain is None:
            self._speak_scripted(
                responder.respond(brief, speech, turn_number, guarded_so_far))
            return
        if responder.is_guarded(speech):
            if guarded_so_far + 1 >= responder.GUARDED_LIMIT:
                self._speak_scripted(responder.Reply(
                    [brief["deflect_line"], brief["closing"]], end=True,
                    reason="guarded_wall", tag="guarded_end"))
            else:
                self._speak_scripted(responder.Reply(
                    [brief["deflect_line"]], tag="deflect"))
            return
        if responder.is_stop_request(speech):
            self._speak_scripted(responder.Reply(
                [brief["closing"]], end=True,
                reason="stop_requested", tag="stop"))
            return
        self._start_generation(turn_number)

    def _on_interrupt(self, event):
        if self.call_id is None or self.brief is None:
            return
        heard = event.get("utteranceUntilInterrupt") or ""
        if not self.gen_done.is_set():
            self.cancel.set()
            self.gen_done.wait(timeout=3)
        if self.utterance_appended:
            transcripts.rewrite_last_muse_turn(
                self.call_id, heard or None, transcripts_dir=self.tdir)
        elif heard:
            transcripts.append_turn(
                self.call_id, "muse", heard, "reply", tag="llm_interrupted",
                transcripts_dir=self.tdir)
        transcripts.append_turn(
            self.call_id, "party", f"interrupt (heard: {heard})", "interrupt",
            transcripts_dir=self.tdir)
        self.utterance_appended = False

    # --- speaking ---------------------------------------------------------
    def _speak_scripted(self, reply):
        """Send a deterministic Reply (rails or the no-brain responder):
        each part as its own completed text message, in order."""
        for part in reply.parts:
            self._send_text(part, last=True)
            transcripts.append_turn(
                self.call_id, "muse", part, "reply", tag=reply.tag,
                transcripts_dir=self.tdir)
        if reply.parts:
            self.utterance_appended = True
        if reply.end:
            self._finish(reply.reason, reply.parts[-1] if reply.parts else "")

    def _join_generation(self, timeout=None):
        if self.gen_thread is not None and self.gen_thread.is_alive():
            self.gen_done.wait(timeout=timeout)

    def _start_generation(self, turn_number):
        brief = self.brief
        turns = transcripts.read_turns(self.call_id, transcripts_dir=self.tdir)
        self.cancel.clear()
        self.gen_done.clear()
        self.utterance_appended = False
        state = {"held": None, "sent_text": ""}
        started = time.monotonic()

        def on_chunk(chunk):
            if self.cancel.is_set():
                raise llm.StreamCancelled()
            if state["held"] is not None:
                self._send_text(state["held"], last=False)
                state["sent_text"] += state["held"]
            state["held"] = chunk

        def work():
            try:
                try:
                    text, end, cancelled = self.brain.stream_reply(
                        brief, turns, on_chunk)
                except llm.LLMError as exc:
                    sys.stderr.write(
                        f"llm stream failed for {self.call_id}: {exc}\n")
                    if self.cancel.is_set():
                        return
                    if state["held"] is not None:
                        self._send_text(state["held"], last=False)
                        state["sent_text"] += state["held"]
                        state["held"] = None
                    self._send_text(brief["fallback_line"], last=True)
                    full = state["sent_text"] + brief["fallback_line"]
                    transcripts.append_turn(
                        self.call_id, "muse", full, "reply", tag="llm_fallback",
                        transcripts_dir=self.tdir)
                    self.utterance_appended = True
                    return
                if cancelled:
                    # The interrupt handler finalizes the transcript
                    # with what the party actually heard.
                    return
                if state["held"] is not None:
                    self._send_text(state["held"], last=True)
                    state["held"] = None
                transcripts.append_turn(
                    self.call_id, "muse", text, "reply", tag="llm",
                    transcripts_dir=self.tdir)
                self.utterance_appended = True
                if end:
                    self._finish("completed", text,
                                 elapsed=time.monotonic() - started)
                elif turn_number >= int(brief["max_turns"]):
                    self._send_text(brief["closing"], last=True)
                    transcripts.append_turn(
                        self.call_id, "muse", brief["closing"], "reply", tag="llm",
                        transcripts_dir=self.tdir)
                    self._finish("max_turns", brief["closing"])
            except Exception as exc:  # socket died mid-send, etc.
                sys.stderr.write(
                    f"relay generation failed for {self.call_id}: {exc}\n")
                self._close_socket()
            finally:
                self.gen_done.set()

        self.gen_thread = threading.Thread(target=work, daemon=True)
        self.gen_thread.start()

    def _finish(self, reason, last_text="", elapsed=0.0):
        """Record the outcome and end the relay session. The end
        message is delayed by a speaking-time estimate for the final
        text (minus time already elapsed since generation started), so
        it cannot cut off the closing line still playing."""
        if not self.outcome_written:
            transcripts.write_outcome(self.call_id, self.brief, reason,
                                      transcripts_dir=self.tdir)
            self.outcome_written = True
        self.over = True
        delay = min(20.0, max(1.0, 1.0 + 0.075 * len(last_text or "") - elapsed))

        def _end():
            time.sleep(delay)
            try:
                self._send_obj({"type": "end"})
            except OSError:
                pass
            time.sleep(0.5)
            self._close_socket()

        self.end_thread = threading.Thread(target=_end, daemon=True)
        self.end_thread.start()


def main():
    parser = argparse.ArgumentParser(description="Twilio outbound calling webhook server")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default 127.0.0.1; use 0.0.0.0 in a container/pod)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--briefs-dir", default=str(brief_store.DEFAULT_BRIEFS_DIR))
    parser.add_argument("--transcripts-dir", default=str(transcripts.DEFAULT_TRANSCRIPTS_DIR))
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), CallHandler)
    server.briefs_dir = Path(args.briefs_dir)
    server.transcripts_dir = Path(args.transcripts_dir)
    server.auth_token = os.environ.get("TWILIO_AUTH_TOKEN") or None
    server.ingest_secret = os.environ.get("BRIEF_INGEST_SECRET") or None
    server.brain = llm.from_env()
    server.relay = None
    if (os.environ.get("VOICE_MODE") or "gather").strip().lower() == "relay":
        server.relay = {
            "tts_provider": os.environ.get("RELAY_TTS_PROVIDER") or "Amazon",
            "voice": os.environ.get("RELAY_VOICE") or "Joanna-Generative",
            "transcription_provider":
                os.environ.get("RELAY_TRANSCRIPTION_PROVIDER") or "",
            "speech_model": os.environ.get("RELAY_SPEECH_MODEL") or "",
            "interruptible": os.environ.get("RELAY_INTERRUPTIBLE") or "any",
            # Twilio's default is "high" (easiest to trigger; TV noise
            # cut in on a live call Nov 12, 2026). "medium" is the
            # balanced step down; "low" needs longer, confident speech.
            "interrupt_sensitivity":
                (os.environ.get("RELAY_INTERRUPT_SENSITIVITY") or "medium")
                .strip().lower()
                if (os.environ.get("RELAY_INTERRUPT_SENSITIVITY") or "medium")
                .strip().lower() in ("high", "medium", "low") else "medium",
        }
    server.briefs_dir.mkdir(parents=True, exist_ok=True)
    server.transcripts_dir.mkdir(parents=True, exist_ok=True)
    print(f"twilio-calling server on http://{args.host}:{args.port} "
          f"(briefs={server.briefs_dir}, transcripts={server.transcripts_dir}, "
          f"signature verification: {'on' if server.auth_token else 'OFF - local dev only'}, "
          f"ingest: {'on' if server.ingest_secret else 'off'}, "
          f"brain: {(server.brain.model + ' @ ' + server.brain.base_url) if server.brain else 'deterministic responder'}, "
          f"voice mode: {('relay (' + server.relay['tts_provider'] + '/' + server.relay['voice'] + ', interrupt ' + server.relay['interrupt_sensitivity'] + ')') if server.relay else 'gather'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
