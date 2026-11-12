#!/usr/bin/env python3
"""Transcript and outcome records for the Twilio outbound calling system.

Every turn of a call is appended as one JSON object per line to
transcripts/<call_id>.jsonl. When a call ends, a summary is written to
transcripts/<call_id>.outcome.json.
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TRANSCRIPTS_DIR = BASE_DIR / "transcripts"

# A call_id becomes a filename. Accept only the same shape briefs.py
# validates, so no caller-supplied value can escape the transcripts dir.
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _check_id(call_id):
    if not SAFE_ID_RE.match(call_id or ""):
        raise ValueError(f"unsafe call_id for a transcript path: {call_id!r}")
    return call_id


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def transcript_path(call_id, transcripts_dir=DEFAULT_TRANSCRIPTS_DIR):
    return Path(transcripts_dir) / f"{_check_id(call_id)}.jsonl"


def outcome_path(call_id, transcripts_dir=DEFAULT_TRANSCRIPTS_DIR):
    return Path(transcripts_dir) / f"{_check_id(call_id)}.outcome.json"


def append_turn(call_id, speaker, text, kind, tag=None, transcripts_dir=DEFAULT_TRANSCRIPTS_DIR):
    """Append one turn to the call's transcript file."""
    path = transcript_path(call_id, transcripts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _now_iso(),
        "call_id": call_id,
        "speaker": speaker,  # "muse" or "party"
        "kind": kind,        # "opening", "speech", "reply", "status"
        "text": text,
    }
    if tag is not None:
        entry["tag"] = tag
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    return entry


def read_turns(call_id, transcripts_dir=DEFAULT_TRANSCRIPTS_DIR):
    path = transcript_path(call_id, transcripts_dir)
    if not path.exists():
        return []
    turns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            turns.append(json.loads(line))
    return turns


def party_turn_count(call_id, transcripts_dir=DEFAULT_TRANSCRIPTS_DIR):
    """Count the party's SPEECH turns. Status callbacks are also logged
    with speaker 'party' but are not conversation turns; counting them
    would burn the call's max_turns budget on carrier events."""
    return sum(
        1
        for t in read_turns(call_id, transcripts_dir)
        if t.get("speaker") == "party" and t.get("kind") == "speech"
    )


def deflect_count(call_id, transcripts_dir=DEFAULT_TRANSCRIPTS_DIR):
    return sum(
        1
        for t in read_turns(call_id, transcripts_dir)
        if t.get("speaker") == "muse" and t.get("tag") in ("deflect", "guarded_end")
    )


def rewrite_last_muse_turn(call_id, new_text, transcripts_dir=DEFAULT_TRANSCRIPTS_DIR):
    """Rewrite the most recent muse reply/opening turn's text in place.

    Used by the ConversationRelay path when the party barges in: the
    transcript (and therefore the history the model sees next) must
    record only what the party actually heard, which Twilio reports as
    utteranceUntilInterrupt. Pass new_text=None to drop the turn
    entirely (nothing was heard). Returns True when a turn was found.
    """
    path = transcript_path(call_id, transcripts_dir)
    if not path.exists():
        return False
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    for i in range(len(entries) - 1, -1, -1):
        entry = entries[i]
        if entry.get("speaker") == "muse" and entry.get("kind") in ("reply", "opening"):
            if new_text is None:
                del entries[i]
            else:
                entry["text"] = new_text
                entry["interrupted"] = True
            path.write_text(
                "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
            )
            return True
    return False


def write_outcome(call_id, brief, reason, transcripts_dir=DEFAULT_TRANSCRIPTS_DIR):
    """Write the end-of-call summary JSON. Returns the outcome dict."""
    turns = read_turns(call_id, transcripts_dir)
    outcome = {
        "call_id": call_id,
        "reason": reason,  # completed / guarded_wall / stop_requested / max_turns / party_hangup
        "to": brief.get("to"),
        "purpose": brief.get("purpose"),
        "party_turns": sum(1 for t in turns if t.get("speaker") == "party"),
        "deflections": sum(
            1 for t in turns if t.get("speaker") == "muse" and t.get("tag") in ("deflect", "guarded_end")
        ),
        "ended_at": _now_iso(),
        "transcript": str(transcript_path(call_id, transcripts_dir)),
    }
    path = outcome_path(call_id, transcripts_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(outcome, indent=2) + "\n", encoding="utf-8")
    return outcome
