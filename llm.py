#!/usr/bin/env python3
"""LLM conversation brain for the Twilio outbound calling system.

When an OpenAI-compatible chat endpoint is configured, this module is
what actually talks: per speech turn it sends the call brief (as the
system prompt) and the conversation so far to the endpoint and returns
what the model says next. In Ernest's deployment the endpoint is his
LiteLLM gateway, so the model behind a call is swappable with one
environment variable (local models on the Spark today, a hosted model
tomorrow) and nothing about a call leaves his network unless his
LiteLLM routes it out. Python standard library only.

Safety shape (see server.py): the deterministic rails run BEFORE this
module is consulted. Guarded personal questions are deflected by rule
and never reach the model, stop requests end the call by rule, and turn
limits are enforced by the server. The brief also contains no personal
details to leak in the first place.

End-of-call contract: the system prompt tells the model to append the
exact marker [[END]] when the call's purpose is achieved or the other
party wraps up. The server strips the marker and hangs up.

Configuration (environment):
  LLM_BASE_URL        required to enable this brain at all, e.g.
                      http://litellm:4000 (the gateway's /chat/completions
                      is appended)
  LLM_MODEL           default gpt-oss:20b (fast tier on the Spark; phone
                      turns are short and latency matters)
  LLM_API_KEY         optional bearer key (LiteLLM master key, when the
                      gateway requires one)
  LLM_TIMEOUT_SECONDS default 10 (Twilio will not wait long for TwiML)
"""
import json
import os
import re
import urllib.error
import urllib.request

END_MARKER = "[[END]]"
DEFAULT_MODEL = "gpt-oss:20b"

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)


class LLMError(Exception):
    """The model call failed; the server falls back to a scripted line."""


class LLMBrain:
    def __init__(self, base_url, model=DEFAULT_MODEL, api_key=None, timeout=10):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
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
        """Chat messages from transcript turns. The opening is folded
        into the system prompt, so history starts with the party."""
        history = [{"role": "system", "content": self.system_prompt(brief)}]
        for turn in turns:
            if turn.get("kind") == "speech" and turn.get("speaker") == "party":
                history.append({"role": "user", "content": turn.get("text", "")})
            elif turn.get("kind") == "reply" and turn.get("speaker") == "muse":
                history.append({"role": "assistant", "content": turn.get("text", "")})
        if len(history) < 2 or history[-1]["role"] != "user":
            raise LLMError("conversation history does not end with a party utterance")
        return history

    # --- the call -------------------------------------------------------
    def reply(self, brief, turns):
        """Return (text, end_call). Raises LLMError on any failure."""
        payload = {
            "model": self.model,
            "max_tokens": 300,
            "temperature": 0.6,
            "messages": self.messages(brief, turns),
        }
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected LLM response shape: {exc}") from exc
        # Some local reasoning models leak their scratchpad into content;
        # never speak it aloud.
        text = _THINK_BLOCK.sub("", text).strip()
        if not text:
            raise LLMError("LLM response carried no speakable text")
        end = END_MARKER in text
        if end:
            text = text.replace(END_MARKER, "").strip()
        if not text:
            raise LLMError("model returned only the end marker")
        return text, end


def from_env(env=None):
    """Build an LLMBrain from the environment, or None when no endpoint
    is configured (the server then uses the deterministic responder)."""
    env = os.environ if env is None else env
    base_url = env.get("LLM_BASE_URL") or None
    if not base_url:
        return None
    try:
        timeout = int(env.get("LLM_TIMEOUT_SECONDS", "10"))
    except ValueError:
        timeout = 10
    return LLMBrain(
        base_url=base_url,
        model=env.get("LLM_MODEL") or DEFAULT_MODEL,
        api_key=env.get("LLM_API_KEY") or None,
        timeout=timeout,
    )
