#!/usr/bin/env python3
"""LLM conversation brain for the Twilio outbound calling system.

When an Anthropic API key is configured, this module is what actually
talks: per speech turn it sends the call brief (as a system prompt) and
the conversation so far to the Anthropic Messages API and returns what
Muse says next. Python standard library only.

Safety shape (see server.py): the deterministic rails run BEFORE this
module is consulted. Guarded personal questions are deflected by rule
and never reach the model, stop requests end the call by rule, and turn
limits are enforced by the server. The brief also contains no personal
details to leak in the first place.

End-of-call contract: the system prompt tells the model to append the
exact marker [[END]] when the call's purpose is achieved or the other
party wraps up. The server strips the marker and hangs up.

Configuration (environment):
  ANTHROPIC_API_KEY   required to enable this brain at all
  ANTHROPIC_MODEL     default claude-haiku-4-5 (fast tier; phone turns
                      are short and latency matters more than depth)
  ANTHROPIC_BASE_URL  default https://api.anthropic.com
  LLM_TIMEOUT_SECONDS default 10 (Twilio will not wait long for TwiML)
"""
import json
import os
import urllib.error
import urllib.request

END_MARKER = "[[END]]"
DEFAULT_MODEL = "claude-haiku-4-5"
DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"


class LLMError(Exception):
    """The model call failed; the server falls back to a scripted line."""


class LLMBrain:
    def __init__(self, api_key, model=DEFAULT_MODEL, base_url=DEFAULT_BASE_URL,
                 timeout=10):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # --- prompt construction ------------------------------------------
    def system_prompt(self, brief):
        facts = "\n".join(f"- {fact['answer']}" for fact in brief.get("facts", []))
        if not facts:
            facts = "(none provided)"
        return f"""You are the caller in a live phone call. {brief["identity"]}

Purpose of this call: {brief["purpose"]}

You already opened the call by saying: "{brief["opening"]}"

Facts you may state. These are the ONLY specific details you know:
{facts}

How to talk:
- Speak naturally, like a person on the phone: one to three short sentences per turn, no lists, no markdown, no form-letter phrasing.
- Ask one question at a time. Answer the other party's questions plainly from the facts above.
- Never invent details that are not in the facts: no names, times, prices, addresses, or confirmations you were not given. If you do not know, say so and offer to confirm and follow up.

Hard rules:
- Never disclose or speculate about personal details concerning your client: no home address, no family members, no children or their ages or schools, no medical or financial information. You do not have any of it. If asked, politely decline once and steer back to the purpose of the call.
- Stay on the purpose of the call. Do not agree to anything beyond it.

Ending the call:
- Your closing line, when the call wraps up, is: "{brief["closing"]}"
- When the purpose is achieved, or the other party clearly says goodbye or wraps up, say your closing line and append the exact marker {END_MARKER} at the very end of your reply. Use the marker only when the call should end."""

    def messages(self, brief, turns):
        """Anthropic messages from transcript turns. The opening is folded
        into the system prompt, so history starts with the party."""
        history = []
        for turn in turns:
            if turn.get("kind") == "speech" and turn.get("speaker") == "party":
                history.append({"role": "user", "content": turn.get("text", "")})
            elif turn.get("kind") == "reply" and turn.get("speaker") == "muse":
                history.append({"role": "assistant", "content": turn.get("text", "")})
        if not history or history[-1]["role"] != "user":
            raise LLMError("conversation history does not end with a party utterance")
        return history

    # --- the call -------------------------------------------------------
    def reply(self, brief, turns):
        """Return (text, end_call). Raises LLMError on any failure."""
        payload = {
            "model": self.model,
            "max_tokens": 300,
            "system": self.system_prompt(brief),
            "messages": self.messages(brief, turns),
        }
        request = urllib.request.Request(
            f"{self.base_url}/v1/messages",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "content-type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": API_VERSION,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"Anthropic request failed: {exc}") from exc
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if block.get("type") == "text"
        ).strip()
        if not text:
            raise LLMError("Anthropic response carried no text")
        end = END_MARKER in text
        if end:
            text = text.replace(END_MARKER, "").strip()
        if not text:
            raise LLMError("model returned only the end marker")
        return text, end


def from_env(env=None):
    """Build an LLMBrain from the environment, or None when no API key is
    configured (the server then uses the deterministic responder)."""
    env = os.environ if env is None else env
    api_key = env.get("ANTHROPIC_API_KEY") or None
    if not api_key:
        return None
    try:
        timeout = int(env.get("LLM_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10
    return LLMBrain(
        api_key=api_key,
        model=env.get("ANTHROPIC_MODEL") or DEFAULT_MODEL,
        base_url=env.get("ANTHROPIC_BASE_URL") or DEFAULT_BASE_URL,
        timeout=timeout,
    )
