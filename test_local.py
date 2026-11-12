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
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import briefs as brief_store  # noqa: E402

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

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) FAILED: {', '.join(FAILURES)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
