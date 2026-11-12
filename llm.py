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

Two call shapes share this module: reply() (one blocking request per
turn, for the TwiML Gather flow) and stream_reply() (SSE streaming with
sentence-sized chunks and mid-stream cancellation, for the
ConversationRelay flow in server.py).

Configuration (environment):
  LLM_BASE_URL        required to enable this brain at all, e.g.
                      http://litellm:4000 (the gateway's /chat/completions
                      is appended)
  LLM_MODEL           default gpt-oss:20b (fast tier on the Spark; phone
                      turns are short and latency matters)
  LLM_API_KEY         optional bearer key (LiteLLM master key, when the
                      gateway requires one)
  LLM_TIMEOUT_SECONDS default 10 (Twilio will not wait long for TwiML)
  LLM_TEMPERATURE     default 0.6. Set to "omit" for models that reject
                      a temperature outright (the Claude 5.x family and
                      Opus 4.7 return 400 for any request carrying one,
                      verified through LiteLLM Nov 12, 2026); with it
                      omitted, switching to those models is env-only.
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


class StreamCancelled(Exception):
    """Raised by a stream chunk callback to abort generation (the
    caller interrupted; nothing further should be spoken)."""


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _clean_stream_text(raw, final):
    """Speakable text from the raw streamed content so far.

    Strips closed <think> blocks, holds an unterminated one back, cuts
    at the end marker, and (unless final) holds back a tail that could
    be the start of a think tag or the end marker. The result is
    prefix-stable as raw grows, which is what makes incremental
    emission safe. Returns (text, end_marker_seen).
    """
    text = _THINK_BLOCK.sub("", raw)
    end = False
    idx = text.find("<think>")
    if idx != -1:
        text = text[:idx]
    idx = text.find(END_MARKER)
    if idx != -1:
        end = True
        text = text[:idx]
    if not final and not end:
        hold = 0
        for marker in ("<think>", END_MARKER):
            for cut in range(len(marker) - 1, 0, -1):
                if text.endswith(marker[:cut]):
                    hold = max(hold, cut)
                    break
        if hold:
            text = text[:-hold]
    return text, end


class LLMBrain:
    def __init__(self, base_url, model=DEFAULT_MODEL, api_key=None, timeout=10,
                 temperature=0.6):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature  # None means: omit the field entirely

    # --- prompt construction ------------------------------------------
    def system_prompt(self, brief):
        facts = "\n".join(f"- {fact['answer']}" for fact in brief.get("facts", []))
        if not facts:
            facts = "(none provided)"
        return f"""You are the caller in a live phone call. {brief["identity"]}

Purpose of this call: {brief["purpose"]}

You already opened the call by saying: "{brief["opening"]}"

Your role, fixed for the whole call: YOU are the caller, the customer side of this conversation. You phoned this business; they did not phone you. You are not their employee, their host, or their booking desk. Never speak as if the other party called you or requested anything from you, never offer them your services, and never ask them to decide something that is yours to decide: whether an option works for your client is your judgment to make within your instructions, not theirs to approve.

Facts you may state. These are the ONLY specific details you know:
{facts}

How to talk:
- Speak naturally, like a person on the phone: one to three short sentences per turn, no lists, no markdown, no form-letter phrasing.
- Ask one question at a time. Answer the other party's questions plainly from the facts above.
- Never invent details that are not in the facts: no names, times, prices, addresses, or confirmations you were not given. If you do not know, say so and offer to confirm and follow up.
- Never repeat a sentence you have already said in this call. If the other party objects, corrects you, or says they already answered, acknowledge it briefly and move forward with something new.

Hard rules:
- Never disclose or speculate about personal details concerning your client: no home address, no family members, no children or their ages or schools, no medical or financial information. You do not have any of it. If asked, politely decline once and steer back to the purpose of the call.
- Stay on the purpose of the call. Do not agree to anything beyond it.
- Your purpose above states exactly what you may agree to, including any specific options you may accept. Never agree to anything outside those instructions, even if the other party insists it is the only option. If nothing within your instructions is available, do not improvise a substitute: politely say you need to check with your client first, thank them, and end the call with your closing line.

Ending the call:
- Your closing line, when the call wraps up, is: "{brief["closing"]}"
- When the purpose is achieved, or the other party clearly says goodbye or wraps up, say your closing line and append the exact marker {END_MARKER} at the very end of your reply. Use the marker only when the call should end."""

    def messages(self, brief, turns):
        """Chat messages from transcript turns. The opening is included
        as the first assistant turn, not just described in the system
        prompt: the model anchors its role far better when it can see
        itself already speaking as the caller from turn one (without
        it, a small model drifts into playing the business it called,
        observed live Nov 12, 2026)."""
        history = [
            {"role": "system", "content": self.system_prompt(brief)},
            {"role": "assistant", "content": brief["opening"]},
        ]
        for turn in turns:
            if turn.get("kind") == "speech" and turn.get("speaker") == "party":
                history.append({"role": "user", "content": turn.get("text", "")})
            elif turn.get("kind") == "reply" and turn.get("speaker") == "muse":
                history.append({"role": "assistant", "content": turn.get("text", "")})
        if len(history) < 2 or history[-1]["role"] != "user":
            raise LLMError("conversation history does not end with a party utterance")
        return history

    # --- the call -------------------------------------------------------
    def _payload(self, brief, turns, stream=False):
        payload = {
            "model": self.model,
            # Generous on purpose: reasoning models served through the
            # gateway can spend part of this budget thinking before any
            # speakable text exists, and a tight cap intermittently
            # returns NO text at all (observed live Nov 12, 2026).
            "max_tokens": 800,
            "messages": self.messages(brief, turns),
        }
        if stream:
            payload["stream"] = True
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        return payload

    def _request(self, payload):
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers=headers,
        )

    def reply(self, brief, turns):
        """Return (text, end_call). Raises LLMError on any failure."""
        request = self._request(self._payload(brief, turns))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"LLM request failed: {exc}") from exc
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"unexpected LLM response shape: {exc}") from exc
        # Some local reasoning models leak their scratchpad into content;
        # never speak it aloud.
        text = _THINK_BLOCK.sub("", text).strip()
        if not text:
            raise LLMError(
                f"LLM response carried no speakable text (finish_reason={finish_reason})"
            )
        end = END_MARKER in text
        if end:
            text = text.replace(END_MARKER, "").strip()
        if not text:
            raise LLMError("model returned only the end marker")
        return text, end

    def stream_reply(self, brief, turns, on_chunk):
        """Stream one reply from the endpoint (OpenAI-style SSE).

        on_chunk(text) is called with sentence-sized chunks of
        speakable text as they become available; it may raise
        StreamCancelled to abort generation (barge-in). Returns
        (emitted_text, end_call, cancelled), where emitted_text is
        exactly the concatenation of the chunks on_chunk accepted.
        Raises LLMError on transport failure or when the model produced
        no speakable text.
        """
        request = self._request(self._payload(brief, turns, stream=True))
        emitted = ""
        pending = ""
        accounted = 0  # chars of the cleaned text already in emitted+pending
        raw = ""
        finish_reason = None

        def emit(chunk):
            nonlocal emitted
            on_chunk(chunk)
            emitted += chunk

        def drain(final, end_seen):
            """Move newly speakable text into chunks. Returns True when
            the end marker was seen and everything is flushed."""
            nonlocal pending, accounted
            clean, end = _clean_stream_text(raw, final=final)
            end = end or end_seen
            if len(clean) > accounted:
                pending += clean[accounted:]
                accounted = len(clean)
            if end or final:
                if pending:
                    emit(pending)
                    pending = ""
            else:
                parts = _SENTENCE_SPLIT.split(pending)
                for part in parts[:-1]:
                    emit(part + " ")
                pending = parts[-1] if parts else ""
            return end

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                end_seen = False
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                        choice = event["choices"][0]
                        piece = (choice.get("delta") or {}).get("content") or ""
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
                    except (KeyError, IndexError, TypeError, json.JSONDecodeError):
                        continue
                    if not piece:
                        continue
                    raw += piece
                    if drain(final=False, end_seen=end_seen):
                        end_seen = True
                        break
                end = drain(final=True, end_seen=end_seen)
        except StreamCancelled:
            return emitted, False, True
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise LLMError(f"LLM stream failed: {exc}") from exc
        if not emitted.strip():
            raise LLMError(
                f"LLM stream carried no speakable text (finish_reason={finish_reason})"
            )
        return emitted, end, False


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
    raw_temp = (env.get("LLM_TEMPERATURE") or "").strip().lower()
    if raw_temp in ("omit", "none", "off"):
        temperature = None
    elif raw_temp:
        try:
            temperature = float(raw_temp)
        except ValueError:
            temperature = 0.6
    else:
        temperature = 0.6
    return LLMBrain(
        base_url=base_url,
        model=env.get("LLM_MODEL") or DEFAULT_MODEL,
        api_key=env.get("LLM_API_KEY") or None,
        timeout=timeout,
        temperature=temperature,
    )
