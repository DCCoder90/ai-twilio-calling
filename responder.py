#!/usr/bin/env python3
"""Deterministic responder for the Twilio outbound calling system.

Given a call brief and one utterance from the other party, decide what
Muse says next and whether the call ends. No external model is used:
the rules below are fixed and auditable, which is what the guardrail in
the design doc requires.

Rule order (first match wins for the reply text):
  (a) Guarded topic  -> deflect_line. The call continues, unless this is
      the GUARDED_LIMIT-th guarded question in the call, in which case
      the party is insisting on a detail Muse cannot give: deflect,
      close politely, end with reason 'guarded_wall'.
  (b) Stop / hostile -> closing, end with reason 'stop_requested'.
  (c) Identity question ("who is calling?") -> brief's identity line.
  (d) Fact match     -> the brief fact whose keywords overlap the
      utterance most (ties go to the earliest fact in the brief).
  (e) Completion signal from the party -> closing, end with reason
      'completed'. Farewell-class signals ("you're all set", "see you
      then", "goodbye") are checked before fact matching, because a
      goodbye sentence usually also mentions the business just done
      ("the table is booked") and must still end the call. Weak
      confirmation words ("confirmed", "booked", "all set") are checked
      after fact matching, since they also appear inside questions
      ("can it be booked for eight?").
  (f) Anything else (including empty speech) -> fallback_line, which
      steers back to the call's purpose.
After any non-ending reply, if the turn count has reached the brief's
max_turns, the closing is appended and the call ends with reason
'max_turns'.
"""
import re
from dataclasses import dataclass, field

# How many guarded questions in one call before Muse wraps up: the call
# evidently cannot proceed without a detail it must never disclose.
GUARDED_LIMIT = 3

# Phrases that mark a guarded topic regardless of single words.
GUARDED_PHRASES = [
    "home address",
    "social security",
    "where does he live",
    "where does she live",
    "where do they live",
    "where he lives",
    "where she lives",
    "where they live",
    "live at",
    "lives at",
    "how old",
    "bank account",
    "credit card",
    "cell number",
    "cell phone number",
    "phone number",
    "his number",
    "her number",
]

# Single words that mark a guarded topic. Deliberately excludes broad
# words like "live" or "number" alone, which appear in innocent
# reservation talk; those are covered by phrases above.
GUARDED_TOKENS = {
    "kid", "kids", "child", "children",
    "daughter", "daughters", "son", "sons",
    "family", "wife", "husband", "spouse",
    "address", "school", "schools",
    "medical", "doctor", "health", "illness", "diagnosis",
    "insurance", "income", "salary", "ssn", "birthday",
    "age", "ages",
}

STOP_PHRASES = [
    "stop calling",
    "do not call",
    "dont call",
    "remove me",
    "take me off",
    "never call",
    "quit calling",
    "stop",
]

IDENTITY_PHRASES = [
    "who is this",
    "who is calling",
    "whos calling",
    "who are you",
    "who am i speaking with",
    "who am i talking to",
    "your name",
    "what company",
]

# Farewell-class completion signals: end the call even if the same
# sentence also matches a brief fact.
COMPLETION_STRONG = [
    "youre all set",
    "you are all set",
    "see you then",
    "goodbye",
    "good bye",
]

# Weak completion signals: only end the call when no fact matched.
COMPLETION_WEAK = [
    "all set",
    "confirmed",
    "booked",
]


def normalize(text):
    """Lowercase, drop apostrophes/punctuation, collapse whitespace."""
    if not text:
        return ""
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _contains_phrase(normalized_text, phrase):
    return f" {phrase} " in f" {normalized_text} "


def _is_guarded(normalized_text):
    if any(_contains_phrase(normalized_text, p) for p in GUARDED_PHRASES):
        return True
    tokens = set(normalized_text.split())
    return bool(tokens & GUARDED_TOKENS)


def _match_fact(brief, normalized_text):
    """Return the brief fact with the most keyword overlap, or None."""
    best = None
    best_overlap = 0
    for fact in brief.get("facts", []):
        overlap = 0
        for keyword in fact.get("keywords", []):
            kw = normalize(keyword)
            if kw and _contains_phrase(normalized_text, kw):
                overlap += 1
        if overlap > best_overlap:
            best = fact
            best_overlap = overlap
    return best


@dataclass
class Reply:
    parts: list = field(default_factory=list)  # lines for Muse to say, in order
    end: bool = False                          # True -> hang up after speaking
    reason: str = None                         # outcome reason when end is True
    tag: str = "fallback"                      # transcript tag for this reply


def respond(brief, speech, turn_number, guarded_count):
    """Decide the next reply.

    brief: validated brief dict (see briefs.py).
    speech: raw SpeechResult text from the other party (may be empty).
    turn_number: 1-based count of party turns including this one.
    guarded_count: guarded questions already deflected earlier in this call.
    """
    norm = normalize(speech)
    deflect = brief["deflect_line"]
    closing = brief["closing"]

    if norm and _is_guarded(norm):
        if guarded_count + 1 >= GUARDED_LIMIT:
            return Reply([deflect, closing], end=True, reason="guarded_wall", tag="guarded_end")
        return Reply([deflect], tag="deflect")

    if norm and any(_contains_phrase(norm, p) for p in STOP_PHRASES):
        return Reply([closing], end=True, reason="stop_requested", tag="stop")

    if norm and any(_contains_phrase(norm, p) for p in COMPLETION_STRONG):
        return Reply([closing], end=True, reason="completed", tag="completed")

    parts = None
    tag = "fallback"
    if norm and any(_contains_phrase(norm, p) for p in IDENTITY_PHRASES):
        parts = [brief["identity"]]
        tag = "identity"
    elif norm:
        fact = _match_fact(brief, norm)
        if fact is not None:
            parts = [fact["answer"]]
            tag = "fact"
        elif any(_contains_phrase(norm, p) for p in COMPLETION_WEAK):
            return Reply([closing], end=True, reason="completed", tag="completed")

    if parts is None:
        parts = [brief["fallback_line"]]
        tag = "fallback"

    if turn_number >= int(brief["max_turns"]):
        return Reply(parts + [closing], end=True, reason="max_turns", tag=tag)

    return Reply(parts, tag=tag)
