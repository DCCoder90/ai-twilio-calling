#!/usr/bin/env python3
"""Webhook server for the Twilio outbound calling system.

Python 3.12 standard library only. Serves the TwiML endpoints Twilio
calls during a live call:

  POST /twilio/voice?call_id=<id>   opening <Say> + speech <Gather>
  POST /twilio/gather?call_id=<id>  one back-and-forth turn
  POST /twilio/status               call status callbacks (logged)

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
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import briefs as brief_store
import responder
import transcripts


def twiml_document(body_inner):
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Response>{body_inner}</Response>'


def say(text):
    return f"<Say>{escape(text)}</Say>"


def gather(call_id, listen_prompt):
    action = escape(f"/twilio/gather?call_id={call_id}", {'"': "&quot;"})
    return (
        f'<Gather input="speech" action="{action}" method="POST" speechTimeout="auto">'
        f"{say(listen_prompt)}</Gather>"
    )


def continue_twiml(call_id, brief, parts):
    body = "".join(say(p) for p in parts) + gather(call_id, brief["listen_prompt"])
    return twiml_document(body)


def end_twiml(parts):
    return twiml_document("".join(say(p) for p in parts) + "<Hangup/>")


def twilio_signature(auth_token, url, form):
    """Twilio's request signature: base64(HMAC-SHA1(token, url + sorted
    name/value pairs of the POST body concatenated without separators))."""
    data = url + "".join(f"{name}{form[name]}" for name in sorted(form))
    mac = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1)
    return base64.b64encode(mac.digest()).decode("ascii")


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

    # --- routes -------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._send(200, "ok", "text/plain; charset=utf-8")
            return
        if parsed.path == "/twilio/voice":
            self._handle_voice(parsed)
            return
        self._send(404, "not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path not in ("/twilio/voice", "/twilio/gather", "/twilio/status"):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        form = self._form()
        if not self._verify_twilio(parsed, form):
            self._send(403, "invalid Twilio signature", "text/plain; charset=utf-8")
            return
        if parsed.path == "/twilio/voice":
            self._handle_voice(parsed)
        elif parsed.path == "/twilio/gather":
            self._handle_gather(parsed, form)
        else:
            self._handle_status(parsed, form)

    # --- handlers -----------------------------------------------------
    def _handle_voice(self, parsed):
        call_id = self._call_id(parsed)
        brief, error = self._load_brief_or_error(call_id)
        if brief is None:
            self._send(200, error)
            return
        transcripts.append_turn(
            call_id, "muse", brief["opening"], "opening",
            transcripts_dir=self.server.transcripts_dir,
        )
        body = say(brief["opening"]) + gather(call_id, brief["listen_prompt"])
        self._send(200, twiml_document(body))

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

        reply = responder.respond(brief, speech, turn_number, guarded_so_far)
        for part in reply.parts:
            transcripts.append_turn(
                call_id, "muse", part, "reply", tag=reply.tag, transcripts_dir=tdir
            )

        if reply.end:
            transcripts.write_outcome(call_id, brief, reply.reason, transcripts_dir=tdir)
            self._send(200, end_twiml(reply.parts))
        else:
            self._send(200, continue_twiml(call_id, brief, reply.parts))

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
    server.briefs_dir.mkdir(parents=True, exist_ok=True)
    server.transcripts_dir.mkdir(parents=True, exist_ok=True)
    print(f"twilio-calling server on http://{args.host}:{args.port} "
          f"(briefs={server.briefs_dir}, transcripts={server.transcripts_dir}, "
          f"signature verification: {'on' if server.auth_token else 'OFF - local dev only'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
