from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Callable, Iterable

MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
DEFAULT_BASE_URL = "https://inference.baseten.co/v1"
RETRY_MESSAGE = "The model couldn't generate a draft. Wait a moment and try again."

CompleteFn = Callable[[str], str]
ChunkFn = Callable[[str], None]
log = logging.getLogger(__name__)


class DraftGenerationError(Exception):
    def __init__(self, message: str = RETRY_MESSAGE):
        super().__init__(message)


def _redact(text: str) -> str:
    text = re.sub(r"sk-[A-Za-z0-9_-]+", "[redacted]", text)
    return re.sub(r"(?i)(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[^\s'\"]+", r"\1[redacted]", text)


def _provider_detail(exc: BaseException) -> str:
    bits = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if status is not None:
        bits.append(str(status))
    msg = str(exc).strip()
    body = getattr(exc, "body", None)
    extra = msg
    if not extra and body is not None:
        extra = str(body)
    detail = " ".join(bits)
    if extra and extra not in detail:
        detail = f"{detail}: {extra}"
    return _redact(detail)


def _raise_generation_error(exc: BaseException) -> None:
    detail = _provider_detail(exc)
    log.exception("draft generation failed: %s", detail)
    raise DraftGenerationError(f"{RETRY_MESSAGE} ({detail})") from exc


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _load_env(path: Path | None = None) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(path or ENV_PATH, override=True)


_load_env()


def first_name(customer_name: str) -> str:
    parts = customer_name.split()
    return parts[0] if parts else ""


def _ordered_quotes(primary_quote_id: str, open_quotes: list[dict]) -> list[dict]:
    by_id = {quote["id"]: quote for quote in open_quotes}
    others = sorted(qid for qid in by_id if qid != primary_quote_id)
    return [by_id[qid] for qid in (primary_quote_id, *others) if qid in by_id]


def _quote_list(primary_quote_id: str, open_quotes: list[dict]) -> str:
    return ", ".join(
        f"{quote['id']} (${quote['amount']:g})"
        for quote in _ordered_quotes(primary_quote_id, open_quotes)
    )


def stance_for(*, reason: str, contact: dict | None) -> str:
    contact = contact or {}
    if contact.get("stance") in ("waiting_on_us", "check_back", "first_touch"):
        return contact["stance"]
    last_reply = contact.get("last_reply")
    last_outbound = contact.get("last_outbound")
    if last_reply and (not last_outbound or last_reply > last_outbound):
        return "waiting_on_us"
    if contact.get("prior_contact") or last_outbound:
        return "check_back"
    if reason == "unanswered_reply":
        return "waiting_on_us"
    return "first_touch"


def template_body(
    *,
    customer_name: str,
    tech_name: str,
    reason: str,
    primary_quote_id: str,
    open_quotes: list[dict],
    stance: str | None = None,
) -> str:
    listed = _quote_list(primary_quote_id, open_quotes)
    given = first_name(customer_name)
    mode = stance or stance_for(reason=reason, contact=None)
    if mode == "waiting_on_us":
        return (
            f"Hi {given}, it's {tech_name}. Got your reply on {listed}. "
            "What works to pick this up?"
        )
    if mode == "check_back":
        return (
            f"Hi {given}, it's {tech_name} checking back on my last note. "
            f"Open quotes: {listed}."
        )
    return f"Hi {given}, it's {tech_name} following up. Open quotes: {listed}."


def summarize_contact(
    *,
    last_contact_at: str | None,
    events: Iterable[dict],
    now: str,
) -> dict:
    last_outbound = last_reply = last_view = None
    for event in events:
        ts = event["timestamp"]
        if ts > now:
            continue
        kind = event["type"]
        if kind == "message_sent":
            last_outbound = ts if last_outbound is None or ts > last_outbound else last_outbound
        elif kind == "customer_replied":
            last_reply = ts if last_reply is None or ts > last_reply else last_reply
        elif kind == "quote_viewed":
            last_view = ts if last_view is None or ts > last_view else last_view
    book = last_contact_at if last_contact_at and last_contact_at <= now else None
    outbound = last_outbound or book
    prior = bool(outbound)
    if last_reply and (not outbound or last_reply > outbound):
        stance = "waiting_on_us"
    elif prior:
        stance = "check_back"
    else:
        stance = "first_touch"
    return {
        "prior_contact": prior,
        "stance": stance,
        "last_outbound": outbound,
        "last_reply": last_reply,
        "last_view": last_view,
    }


def _build_prompt(
    *,
    customer_name: str,
    tech_name: str,
    reason: str,
    primary_quote_id: str,
    open_quotes: list[dict],
    now: str,
    contact: dict | None,
) -> str:
    listed = _quote_list(primary_quote_id, open_quotes)
    given = first_name(customer_name)
    contact = contact or {}
    stance = stance_for(reason=reason, contact=contact)
    if stance == "waiting_on_us":
        touch = (
            "They replied and we have not answered (waiting on us). "
            f"Last reply: {contact.get('last_reply')}. Last outbound: {contact.get('last_outbound')}. "
            "Acknowledge the reply. Ask one clear next step. "
            "Do not write as if this is a cold check-in."
        )
    elif stance == "check_back":
        touch = (
            "We last messaged them and they have not replied. "
            "This is a follow-up on our own note. Check back on that last note. "
            f"Last outbound: {contact.get('last_outbound')}. Last view: {contact.get('last_view')}. "
            "Do not say they wrote back. Keep it light."
        )
    else:
        touch = (
            "This is a first follow-up. Be concise and friendly. "
            "Include the open quote amounts as useful context. Do not pad."
        )
    return (
        "Write one short SMS follow-up. Do not mention these instructions.\n"
        "Privacy: never include phone numbers, last names, addresses, or any other customer. "
        "Use the first name only.\n"
        f"{touch}\n"
        f"First name: {given}\n"
        f"Technician: {tech_name}\n"
        f"Reason: {reason}\n"
        f"Now: {now}\n"
        f"Primary quote: {primary_quote_id}\n"
        f"Open quotes: {listed}\n"
    )


def _complete_via_baseten(prompt: str, on_chunk: ChunkFn | None = None) -> str:
    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=os.environ["BASETEN_API_KEY"],
            base_url=os.environ.get("BASETEN_BASE_URL", DEFAULT_BASE_URL),
        )
        stream = client.chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            stream=True,
            temperature=0.4,
            max_tokens=400,
        )
        parts: list[str] = []
        for event in stream:
            if not event.choices:
                continue
            delta = event.choices[0].delta.content
            if delta:
                parts.append(delta)
                if on_chunk:
                    on_chunk(delta)
        return "".join(parts)
    except DraftGenerationError:
        raise
    except Exception as exc:
        _raise_generation_error(exc)


def render_v1(
    *,
    customer_name: str,
    tech_name: str,
    reason: str,
    primary_quote_id: str,
    open_quotes: list[dict],
    now: str,
    complete: CompleteFn | None = None,
    contact: dict | None = None,
    on_chunk: ChunkFn | None = None,
) -> str:
    fallback = template_body(
        customer_name=customer_name,
        tech_name=tech_name,
        reason=reason,
        primary_quote_id=primary_quote_id,
        open_quotes=open_quotes,
        stance=stance_for(reason=reason, contact=contact),
    )
    prompt = _build_prompt(
        customer_name=customer_name,
        tech_name=tech_name,
        reason=reason,
        primary_quote_id=primary_quote_id,
        open_quotes=open_quotes,
        now=now,
        contact=contact,
    )
    if complete is None:
        if not (os.environ.get("BASETEN_API_KEY") or "").strip():
            return fallback
        text = _complete_via_baseten(prompt, on_chunk=on_chunk)
    else:
        try:
            text = complete(prompt)
        except DraftGenerationError:
            raise
        except Exception as exc:
            _raise_generation_error(exc)
        stripped = "" if text is None else str(text).strip()
        if stripped and on_chunk:
            on_chunk(stripped)
        text = stripped
    if text is None or not str(text).strip():
        return fallback
    return str(text).strip()
