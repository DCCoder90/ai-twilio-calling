#!/usr/bin/env python3
"""Local end-to-end tests for the Twilio outbound calling server.

Starts server.py on a local port with throwaway brief/transcript dirs,
loads a sample reservation brief, and simulates Twilio's webhooks with
plain HTTP form posts. No network beyond 127.0.0.1, no Twilio account.

Run: python3 test_local.py
Exit code 0 means every check passed.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import briefs as brief_store  # noqa: E402
import llm as llm_mod  # noqa: E402
import transcripts as transcripts_mod  # noqa: E402

PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"

SAMPLE_BRIEF = {
    "to": "+15555550199",
    "from_number": "+15555550100",
    "opening": (
        "Hello, this is Muse, calling on behalf of my client to make a "
        "dinner reservation for Friday evening, party of four, around seven."
    ),
    "identity": (
        "I'm Muse, an assistant calling on behalf of the client who asked "
        "me to make this reservation."
    ),
    "purpose": "Make a dinner reservation for Friday, party of four, around 7 PM.",
    "facts": [
        {
            "keywords": ["hours", "open", "close", "when"],
            "answer": "The booking notes say you are open until ten PM on Fridays.",
        },
        {
            "keywords": ["parking", "park", "garage"],
            "answer": "Good to know about parking, thank you. The party will arrive by car.",
        },
        {
            "keywords": ["party", "people", "guests", "table"],
            "answer": "It is a party of four, one table is fine.",
        },
    ],
    "closing": "Thank you very much for your help. Goodbye.",
    "deflect_line": "I'm not able to share that. Let's stick to the reservation.",
    "fallback_line": "Understood. To keep us on track, I'm calling to arrange the reservation for Friday, party of four, around seven.",
    "max_turns": 8,
}

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def post(path, form=None):
    data = urllib.parse.urlencode(form or {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read().decode("utf-8")


def post_code(base, path, form=None, headers=None):
    """POST and return just the HTTP status, including error statuses."""
    data = urllib.parse.urlencode(form or {}).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def wait_for_server(proc, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"server exited early with code {proc.returncode}")
        try:
            req = urllib.request.Request(BASE + "/health", method="GET")
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server did not become ready in time")


def main():
    tmp = tempfile.mkdtemp(prefix="twilio-calling-test-")
    briefs_dir = Path(tmp) / "briefs"
    transcripts_dir = Path(tmp) / "transcripts"

    proc = subprocess.Popen(
        [
            sys.executable, str(HERE / "server.py"),
            "--port", str(PORT),
            "--briefs-dir", str(briefs_dir),
            "--transcripts-dir", str(transcripts_dir),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_for_server(proc)

        # Sample brief, created through the brief store like real use.
        call_main = brief_store.create_brief(SAMPLE_BRIEF, call_id="test-reservation",
                                             briefs_dir=briefs_dir)
        check("brief store creates and validates the sample brief", call_main == "test-reservation")

        # (1) Answer webhook returns the opening + a speech Gather.
        status, body = post(f"/twilio/voice?call_id={call_main}")
        check("voice webhook returns 200", status == 200)
        check("voice webhook speaks the brief opening", SAMPLE_BRIEF["opening"] in body)

        quiet_brief = dict(SAMPLE_BRIEF, listen_prompt="")
        call_quiet = "test-quiet"
        brief_store.create_brief(quiet_brief, call_id=call_quiet, briefs_dir=briefs_dir)
        status, body = post(f"/twilio/voice?call_id={call_quiet}")
        check("empty listen_prompt yields a bare Gather (no spoken prompt)",
              '<Gather input="speech"' in body and body.count("<Say>") == 1)
        check("voice webhook opens a speech Gather",
              "<Gather" in body and 'input="speech"' in body and "speechTimeout=" in body)
        check("voice webhook does not hang up", "<Hangup" not in body)

        # (2) A factual question returns the brief's answer, call continues.
        status, body = post(f"/twilio/gather?call_id={call_main}",
                            {"SpeechResult": "Do you have parking available?"})
        check("factual question returns the brief's parking answer",
              SAMPLE_BRIEF["facts"][1]["answer"] in body)
        check("call continues after a factual answer",
              "<Gather" in body and "<Hangup" not in body)

        # (3) Guarded personal questions deflect and do NOT end the call.
        call_guard = brief_store.create_brief(SAMPLE_BRIEF, call_id="test-guarded",
                                              briefs_dir=briefs_dir)
        post(f"/twilio/voice?call_id={call_guard}")
        status, body = post(f"/twilio/gather?call_id={call_guard}",
                            {"SpeechResult": "What's his home address?"})
        check("guarded question 1 (home address) returns the deflect line",
              SAMPLE_BRIEF["deflect_line"] in body)
        check("guarded question 1 does not end the call",
              "<Gather" in body and "<Hangup" not in body)
        status, body = post(f"/twilio/gather?call_id={call_guard}",
                            {"SpeechResult": "How old are his kids?"})
        check("guarded question 2 (kids' ages) returns the deflect line",
              SAMPLE_BRIEF["deflect_line"] in body)
        check("guarded question 2 does not end the call",
              "<Gather" in body and "<Hangup" not in body)
        check("guarded replies never leak a closing/hangup TwiML combo",
              "<Hangup" not in body)

        # (4) A completion phrase ends the call: closing + Hangup,
        #     and transcript + outcome files are written.
        status, body = post(f"/twilio/gather?call_id={call_main}",
                            {"SpeechResult": "You're all set, the table is booked. See you then."})
        check("completion phrase returns the closing line",
              SAMPLE_BRIEF["closing"] in body)
        check("completion phrase hangs up", "<Hangup/>" in body)
        check("completion ends the Gather loop", "<Gather" not in body)

        transcript_file = transcripts_dir / f"{call_main}.jsonl"
        outcome_file = transcripts_dir / f"{call_main}.outcome.json"
        check("transcript file written", transcript_file.exists())
        check("outcome file written", outcome_file.exists())
        if transcript_file.exists():
            turns = [json.loads(line) for line in transcript_file.read_text().splitlines() if line.strip()]
            speakers = [t["speaker"] for t in turns]
            check("transcript records opening, party speech, and Muse replies",
                  speakers[0] == "muse" and "party" in speakers and speakers.count("muse") >= 3,
                  detail=f"speakers={speakers}")
            check("transcript entries carry timestamps", all("ts" in t for t in turns))
        if outcome_file.exists():
            outcome = json.loads(outcome_file.read_text())
            check("outcome reason is 'completed'", outcome.get("reason") == "completed",
                  detail=f"outcome={outcome}")
            check("outcome records destination and party turns",
                  outcome.get("to") == SAMPLE_BRIEF["to"] and outcome.get("party_turns") == 2)

        # Status callback is accepted and logged.
        status, _ = post("/twilio/status?call_id=test-status",
                         {"CallSid": "CA-test", "CallStatus": "ringing",
                          "From": SAMPLE_BRIEF["from_number"], "To": SAMPLE_BRIEF["to"]})
        check("status callback returns 200", status == 200)
        check("status callback logged to transcript dir",
              (transcripts_dir / "test-status.jsonl").exists())
        check("status callbacks do not count as party speech turns",
              transcripts_mod.party_turn_count("test-status", transcripts_dir) == 0)
        check("party speech turns are counted exactly",
              transcripts_mod.party_turn_count(call_main, transcripts_dir) == 2)

        # A hostile call_id must not name files outside the transcripts
        # dir (the status route used to take it unchecked).
        evil = urllib.parse.quote("../../evilprobe", safe="")
        code = post_code(BASE, f"/twilio/status?call_id={evil}",
                         {"CallSid": "CA-test", "CallStatus": "ringing"})
        check("status with traversal call_id is rejected (400)", code == 400)
        check("traversal call_id wrote no file outside the transcripts dir",
              not (Path(tmp) / "evilprobe.jsonl").exists()
              and not (transcripts_dir / "evilprobe.jsonl").exists())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    # Signature suite: a second instance with TWILIO_AUTH_TOKEN set must
    # reject unsigned requests and accept correctly signed ones.
    port2 = 8792
    base2 = f"http://127.0.0.1:{port2}"
    briefs2 = Path(tmp) / "briefs2"
    transcripts2 = Path(tmp) / "transcripts2"
    token = "test-secret-token"
    env = dict(os.environ, TWILIO_AUTH_TOKEN=token)
    proc2 = subprocess.Popen(
        [
            sys.executable, str(HERE / "server.py"),
            "--port", str(port2),
            "--briefs-dir", str(briefs2),
            "--transcripts-dir", str(transcripts2),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        deadline = time.time() + 15
        while True:
            try:
                with urllib.request.urlopen(base2 + "/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("signature-suite server did not start")
                time.sleep(0.2)
        brief_store.create_brief(SAMPLE_BRIEF, call_id="sig-test", briefs_dir=briefs2)

        code = post_code(base2, "/twilio/voice?call_id=sig-test")
        check("unsigned webhook POST is rejected when a token is set (403)", code == 403)

        url = f"{base2}/twilio/voice?call_id=sig-test"
        mac = hmac.new(token.encode(), url.encode(), hashlib.sha1)
        signature = base64.b64encode(mac.digest()).decode()
        req = urllib.request.Request(url, data=b"", method="POST",
                                     headers={"X-Twilio-Signature": signature})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            code = resp.status
        check("correctly signed webhook POST is accepted (200)", code == 200)
        check("signed voice webhook speaks the opening", SAMPLE_BRIEF["opening"] in body)
    finally:
        proc2.terminate()
        try:
            proc2.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc2.kill()

    # LLM suite: a third instance pointed at a local stub of an
    # OpenAI-compatible chat endpoint (the LiteLLM shape). Checks the
    # model path, the [[END]] contract, the deterministic guardrail
    # running BEFORE the model, and the scripted fallback when the
    # model call fails.
    stub_state = {"count": 0, "fail": False}
    stub_port = 8793

    class StubChat(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            stub_state["count"] += 1
            if stub_state["fail"]:
                body = b'{"error":{"message":"stub down","type":"api_error"}}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            payload = json.loads(raw.decode("utf-8"))
            last_user = ""
            for msg in reversed(payload.get("messages", [])):
                if msg.get("role") == "user":
                    last_user = msg.get("content", "")
                    break
            if "wrapup" in last_user.lower():
                text = "All set then. " + SAMPLE_BRIEF["closing"] + " [[END]]"
            else:
                text = "Stub model here, happy to keep talking."
            body = json.dumps({
                "id": "chatcmpl_stub", "object": "chat.completion",
                "model": payload.get("model", "stub"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
                "usage": {},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), StubChat)
    threading.Thread(target=stub.serve_forever, daemon=True).start()

    port3 = 8794
    base3 = f"http://127.0.0.1:{port3}"
    briefs3 = Path(tmp) / "briefs3"
    transcripts3 = Path(tmp) / "transcripts3"
    env3 = dict(os.environ,
                LLM_BASE_URL=f"http://127.0.0.1:{stub_port}",
                LLM_MODEL="stub-model")
    env3.pop("TWILIO_AUTH_TOKEN", None)
    proc3 = subprocess.Popen(
        [
            sys.executable, str(HERE / "server.py"),
            "--port", str(port3),
            "--briefs-dir", str(briefs3),
            "--transcripts-dir", str(transcripts3),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env3,
    )
    try:
        deadline = time.time() + 15
        while True:
            try:
                with urllib.request.urlopen(base3 + "/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("llm-suite server did not start")
                time.sleep(0.2)

        def post3(path, form=None):
            data = urllib.parse.urlencode(form or {}).encode("utf-8")
            req = urllib.request.Request(base3 + path, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8")

        brain_unit = llm_mod.LLMBrain("http://127.0.0.1:9", "unit-model")
        unit_msgs = brain_unit.messages(
            SAMPLE_BRIEF, [{"kind": "speech", "speaker": "party", "text": "Hello?"}]
        )
        check("llm messages anchor the opening as the first assistant turn",
              unit_msgs[1]["role"] == "assistant"
              and unit_msgs[1]["content"] == SAMPLE_BRIEF["opening"])
        check("llm messages put the party last as the user",
              unit_msgs[-1]["role"] == "user" and unit_msgs[-1]["content"] == "Hello?")

        captured = {}

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({
                    "choices": [{"message": {"content": "unit reply"},
                                 "finish_reason": "stop"}]
                }).encode("utf-8")

        def _fake_urlopen(req, timeout=None):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _FakeResp()

        real_urlopen = llm_mod.urllib.request.urlopen
        llm_mod.urllib.request.urlopen = _fake_urlopen
        try:
            unit_turns = [{"kind": "speech", "speaker": "party", "text": "Hi"}]
            brain_unit.reply(SAMPLE_BRIEF, unit_turns)
            check("llm payload sends temperature 0.6 by default",
                  captured["payload"].get("temperature") == 0.6)
            brain_omit = llm_mod.LLMBrain("http://127.0.0.1:9", "unit-model",
                                          temperature=None)
            brain_omit.reply(SAMPLE_BRIEF, unit_turns)
            check("llm payload omits temperature when configured (5.x models reject it)",
                  "temperature" not in captured["payload"])
            env_omit = llm_mod.from_env({"LLM_BASE_URL": "http://127.0.0.1:9",
                                         "LLM_TEMPERATURE": "omit"})
            check("LLM_TEMPERATURE=omit parses to no temperature",
                  env_omit is not None and env_omit.temperature is None)
        finally:
            llm_mod.urllib.request.urlopen = real_urlopen

        call_llm = brief_store.create_brief(SAMPLE_BRIEF, call_id="llm-chat",
                                            briefs_dir=briefs3)
        status, body = post3(f"/twilio/voice?call_id={call_llm}")
        check("llm mode: voice webhook speaks the opening",
              status == 200 and SAMPLE_BRIEF["opening"] in body)

        status, body = post3(f"/twilio/gather?call_id={call_llm}",
                             {"SpeechResult": "Hello, who is this?"})
        check("llm mode: model reply is spoken", "Stub model here" in body)
        check("llm mode: call continues after a model reply", "<Gather" in body)
        check("llm mode: model was called exactly once so far", stub_state["count"] == 1)

        status, body = post3(f"/twilio/gather?call_id={call_llm}",
                             {"SpeechResult": "What is his home address?"})
        check("llm mode: guarded question gets the scripted deflect line",
              SAMPLE_BRIEF["deflect_line"] in body)
        check("llm mode: guarded question never reached the model",
              stub_state["count"] == 1)
        check("llm mode: call continues after deflection", "<Gather" in body)

        status, body = post3(f"/twilio/gather?call_id={call_llm}",
                             {"SpeechResult": "Okay, wrapup now."})
        check("llm mode: end marker hangs up", "<Hangup/>" in body)
        check("llm mode: end marker is stripped from speech", "[[END]]" not in body)
        outcome3 = transcripts3 / f"{call_llm}.outcome.json"
        check("llm mode: outcome file written", outcome3.exists())
        if outcome3.exists():
            check("llm mode: outcome reason is 'completed'",
                  json.loads(outcome3.read_text()).get("reason") == "completed")

        call_fail = brief_store.create_brief(SAMPLE_BRIEF, call_id="llm-fail",
                                             briefs_dir=briefs3)
        post3(f"/twilio/voice?call_id={call_fail}")
        stub_state["fail"] = True
        status, body = post3(f"/twilio/gather?call_id={call_fail}",
                             {"SpeechResult": "Hello?"})
        check("llm mode: model failure speaks the fallback line",
              SAMPLE_BRIEF["fallback_line"] in body)
        check("llm mode: call continues after a model failure", "<Gather" in body)
        stub_state["fail"] = False
    finally:
        proc3.terminate()
        try:
            proc3.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc3.kill()
        stub.shutdown()

    # Ingest suite: a fourth instance with BRIEF_INGEST_SECRET set.
    # Muse's hands-free flow: post an approved brief, dial against it,
    # fetch the transcript afterward. Auth on both routes, validation,
    # and the spent-call_id rule are all exercised.
    port4 = 8794
    base4 = f"http://127.0.0.1:{port4}"
    briefs4 = Path(tmp) / "briefs4"
    transcripts4 = Path(tmp) / "transcripts4"
    ingest_secret = "ingest-test-secret"
    env4 = dict(os.environ, BRIEF_INGEST_SECRET=ingest_secret)
    proc4 = subprocess.Popen(
        [
            sys.executable, str(HERE / "server.py"),
            "--port", str(port4),
            "--briefs-dir", str(briefs4),
            "--transcripts-dir", str(transcripts4),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env4,
    )

    def raw(method, base, path, payload=None, headers=None):
        data = None
        hdrs = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        req = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    try:
        deadline = time.time() + 15
        while True:
            try:
                with urllib.request.urlopen(base4 + "/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("ingest-suite server did not start")
                time.sleep(0.2)

        good = {"X-Ingest-Secret": ingest_secret}
        code, _ = raw("POST", base4, "/briefs",
                      {"call_id": "ingest-1", "brief": SAMPLE_BRIEF})
        check("ingest without the secret is refused (401)", code == 401)
        code, _ = raw("POST", base4, "/briefs",
                      {"call_id": "ingest-1", "brief": SAMPLE_BRIEF},
                      {"X-Ingest-Secret": "wrong"})
        check("ingest with the wrong secret is refused (401)", code == 401)
        code, _ = raw("POST", base4, "/briefs",
                      {"call_id": "ingest-1", "brief": {"to": "+1"}}, good)
        check("ingest of an invalid brief is refused (400)", code == 400)
        code, body = raw("POST", base4, "/briefs",
                         {"call_id": "ingest-1", "brief": SAMPLE_BRIEF}, good)
        check("ingest of a valid brief succeeds (200)", code == 200, body)

        req = urllib.request.Request(base4 + "/twilio/voice?call_id=ingest-1",
                                     data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            vbody = resp.read().decode("utf-8")
        check("ingested brief serves the voice webhook", SAMPLE_BRIEF["opening"] in vbody)
        code, _ = raw("GET", base4, "/transcripts/ingest-1")
        check("transcript fetch without the secret is refused (401)", code == 401)
        code, tbody = raw("GET", base4, "/transcripts/ingest-1", None, good)
        tdata = json.loads(tbody) if code == 200 else {}
        check("transcript fetch with the secret returns the turns",
              code == 200 and any(t.get("kind") == "opening" for t in tdata.get("turns", [])))
        code, _ = raw("POST", base4, "/briefs",
                      {"call_id": "ingest-1", "brief": SAMPLE_BRIEF}, good)
        check("re-ingesting a spent call_id is refused (409)", code == 409)
    finally:
        proc4.terminate()
        try:
            proc4.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc4.kill()

    # Relay suite: a fifth instance in ConversationRelay mode
    # (VOICE_MODE=relay) against a streaming-capable stub gateway.
    # Exercises the WebSocket handshake, the relay TwiML, a streamed
    # model reply, the guardrail running before the model, barge-in
    # heard-tracking on both paths (completed playback -> transcript
    # rewrite; mid-generation -> cancel + heard append), and the
    # [[END]] -> end message + outcome flow.
    relay_stub_state = {"count": 0, "requests": []}
    relay_stub_port = 8797
    SLOW_TEXT = ("First sentence here. Second sentence follows. "
                 "Third sentence arrives. Fourth sentence continues. "
                 "Fifth sentence concludes.")

    class StubChat2(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw_body.decode("utf-8"))
            relay_stub_state["count"] += 1
            relay_stub_state["requests"].append(payload.get("messages", []))
            last_user = ""
            for msg in reversed(payload.get("messages", [])):
                if msg.get("role") == "user":
                    last_user = msg.get("content", "")
                    break
            low = last_user.lower()
            if "wrapup" in low:
                text = "All set then. " + SAMPLE_BRIEF["closing"] + " [[END]]"
                slow = False
            elif "slow" in low:
                text = SLOW_TEXT
                slow = True
            else:
                text = "Stub model here, happy to keep talking."
                slow = False
            if payload.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                chunks = [c for c in re.findall(r"[^.!?]+[.!?]\s*|[^.!?]+$", text)
                          if c.strip()]
                try:
                    for chunk in chunks:
                        event = {"choices": [{"delta": {"content": chunk},
                                              "finish_reason": None}]}
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                        self.wfile.flush()
                        if slow:
                            time.sleep(0.25)
                    done = {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                    self.wfile.write(f"data: {json.dumps(done)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            body = json.dumps({
                "id": "chatcmpl_stub2", "object": "chat.completion",
                "model": payload.get("model", "stub"),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": text},
                    "finish_reason": "stop",
                }],
                "usage": {},
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    stub2 = ThreadingHTTPServer(("127.0.0.1", relay_stub_port), StubChat2)
    threading.Thread(target=stub2.serve_forever, daemon=True).start()

    class WSClient:
        """Minimal WebSocket client for the relay endpoint."""

        def __init__(self, port, path):
            self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
            key = base64.b64encode(os.urandom(16)).decode()
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            self.sock.sendall(request.encode())
            self.buf = b""
            while b"\r\n\r\n" not in self.buf:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("handshake closed")
                self.buf += data
            head, self.buf = self.buf.split(b"\r\n\r\n", 1)
            self.status_line = head.decode("iso-8859-1").splitlines()[0]

        def send_obj(self, obj):
            payload = json.dumps(obj).encode()
            mask = os.urandom(4)
            n = len(payload)
            if n < 126:
                head = bytes([0x81, 0x80 | n])
            elif n <= 0xFFFF:
                head = bytes([0x81, 0xFE]) + struct.pack(">H", n)
            else:
                head = bytes([0x81, 0xFF]) + struct.pack(">Q", n)
            masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            self.sock.sendall(head + mask + masked)

        def _read_exact(self, n):
            while len(self.buf) < n:
                data = self.sock.recv(4096)
                if not data:
                    raise ConnectionError("socket closed")
                self.buf += data
            out, self.buf = self.buf[:n], self.buf[n:]
            return out

        def recv_obj(self, timeout=10):
            self.sock.settimeout(timeout)
            chunks = []
            while True:
                b0, b1 = self._read_exact(2)
                fin = bool(b0 & 0x80)
                opcode = b0 & 0x0F
                length = b1 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._read_exact(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._read_exact(8))[0]
                payload = self._read_exact(length) if length else b""
                if opcode == 0x9:  # ping -> pong
                    self.sock.sendall(bytes([0x8A, len(payload)]) + payload)
                    continue
                if opcode == 0x8:
                    return None
                chunks.append(payload)
                if fin:
                    return json.loads(b"".join(chunks).decode("utf-8"))

        def read_until(self, pred, timeout=15):
            seen = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                obj = self.recv_obj(timeout=max(1.0, deadline - time.time()))
                if obj is None:
                    break
                seen.append(obj)
                if pred(obj):
                    break
            return seen

        def close(self):
            try:
                self.sock.close()
            except OSError:
                pass

    port5 = 8798
    base5 = f"http://127.0.0.1:{port5}"
    briefs5 = Path(tmp) / "briefs5"
    transcripts5 = Path(tmp) / "transcripts5"
    env5 = dict(os.environ,
                LLM_BASE_URL=f"http://127.0.0.1:{relay_stub_port}",
                LLM_MODEL="stub-model", VOICE_MODE="relay")
    env5.pop("TWILIO_AUTH_TOKEN", None)
    proc5 = subprocess.Popen(
        [
            sys.executable, str(HERE / "server.py"),
            "--port", str(port5),
            "--briefs-dir", str(briefs5),
            "--transcripts-dir", str(transcripts5),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env5,
    )
    try:
        deadline = time.time() + 15
        while True:
            try:
                with urllib.request.urlopen(base5 + "/health", timeout=2) as resp:
                    if resp.status == 200:
                        break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("relay-suite server did not start")
                time.sleep(0.2)

        def voice5(call_id):
            req = urllib.request.Request(base5 + f"/twilio/voice?call_id={call_id}",
                                         data=b"", method="POST")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.read().decode("utf-8")

        def assemble(msgs):
            return "".join(m.get("token", "") for m in msgs
                           if m.get("type") == "text")

        def heard_after(requests, needle):
            """Assistant content immediately after the user turn whose
            text contains needle, in the most recent stub request."""
            msgs = requests[-1]
            for i, m in enumerate(msgs):
                if m.get("role") == "user" and needle in m.get("content", ""):
                    if i + 1 < len(msgs):
                        return msgs[i + 1].get("content")
            return None

        call_relay = brief_store.create_brief(SAMPLE_BRIEF, call_id="relay-chat",
                                              briefs_dir=briefs5)
        body = voice5(call_relay)
        check("relay mode: voice webhook returns ConversationRelay TwiML",
              "<ConversationRelay" in body and "/twilio/relay" in body)
        check("relay mode: opening rides as the welcome greeting",
              "welcomeGreeting=" in body and SAMPLE_BRIEF["opening"][:24] in body)
        check("relay mode: no Gather loop in relay TwiML", "<Gather" not in body)

        ws = WSClient(port5, "/twilio/relay")
        check("relay websocket handshake is 101", "101" in ws.status_line)
        ws.send_obj({"type": "setup", "callSid": "CA-relay-a",
                     "customParameters": {"call_id": call_relay}})
        ws.send_obj({"type": "prompt", "voicePrompt": "Hello, who is this?",
                     "last": True})
        msgs = ws.read_until(lambda m: m.get("type") == "text" and m.get("last") is True)
        check("relay: streamed model reply assembles to the stub text",
              assemble(msgs).strip() == "Stub model here, happy to keep talking.")
        count_after_first = relay_stub_state["count"]

        ws.send_obj({"type": "prompt", "voicePrompt": "What is his home address?",
                     "last": True})
        msgs = ws.read_until(lambda m: m.get("type") == "text" and m.get("last") is True)
        check("relay: guarded question gets the deflect line",
              assemble(msgs).strip() == SAMPLE_BRIEF["deflect_line"])
        check("relay: guarded question never reached the model",
              relay_stub_state["count"] == count_after_first)

        # Barge-in over completed playback: the deflect turn in the
        # transcript (and so in the next request's history) must be
        # rewritten to only what the party heard.
        ws.send_obj({"type": "interrupt",
                     "utteranceUntilInterrupt": "I'm not able",
                     "durationUntilInterruptMs": 300})
        ws.send_obj({"type": "prompt", "voicePrompt": "Okay wrapup now.",
                     "last": True})
        msgs = ws.read_until(lambda m: m.get("type") == "end", timeout=25)
        check("relay: end message follows the closing reply",
              any(m.get("type") == "end" for m in msgs))
        check("relay: interrupted playback rewrites history to what was heard",
              heard_after(relay_stub_state["requests"], "home address") == "I'm not able")
        outcome5 = transcripts5 / f"{call_relay}.outcome.json"
        check("relay: outcome written with reason 'completed'",
              outcome5.exists()
              and json.loads(outcome5.read_text()).get("reason") == "completed")
        ws.close()

        # Barge-in mid-generation: cancel the stream and record only
        # the heard prefix for the next turn's history.
        call_slow = brief_store.create_brief(SAMPLE_BRIEF, call_id="relay-slow",
                                             briefs_dir=briefs5)
        voice5(call_slow)
        ws2 = WSClient(port5, "/twilio/relay")
        ws2.send_obj({"type": "setup", "callSid": "CA-relay-b",
                      "customParameters": {"call_id": call_slow}})
        ws2.send_obj({"type": "prompt",
                      "voicePrompt": "Tell me something slow please.", "last": True})
        first = ws2.read_until(lambda m: m.get("type") == "text", timeout=10)
        check("relay: slow reply starts streaming",
              any(m.get("type") == "text" for m in first))
        ws2.send_obj({"type": "interrupt",
                      "utteranceUntilInterrupt": "First sentence here.",
                      "durationUntilInterruptMs": 400})
        ws2.send_obj({"type": "prompt", "voicePrompt": "Okay wrapup now.",
                      "last": True})
        msgs = ws2.read_until(lambda m: m.get("type") == "end", timeout=25)
        check("relay: session ends cleanly after a mid-stream interrupt",
              any(m.get("type") == "end" for m in msgs))
        check("relay: mid-stream interrupt truncates history to the heard text",
              heard_after(relay_stub_state["requests"], "slow")
              == "First sentence here.")
        turns_b = transcripts_mod.read_turns(call_slow, transcripts5)
        check("relay: interrupt is recorded in the transcript",
              any(t.get("kind") == "interrupt" for t in turns_b))
        check("relay: the unheard tail of an interrupted reply is not recorded",
              not any(t.get("kind") == "reply" and "Fifth sentence" in t.get("text", "")
                      for t in turns_b))
        ws2.close()
    finally:
        proc5.terminate()
        try:
            proc5.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc5.kill()
        stub2.shutdown()

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
