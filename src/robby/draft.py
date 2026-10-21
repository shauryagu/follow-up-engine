from __future__ import annotations

import os
from typing import Callable, Iterable

MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
DEFAULT_BASE_URL = "https://inference.baseten.co/v1"
RETRY_MESSAGE = "The model couldn't generate a draft. Wait a moment and try again."

CompleteFn = Callable[[str], str]
ChunkFn = Callable[[str], None]


class DraftGenerationError(Exception):
    def __init__(self, message: str = RETRY_MESSAGE):
        super().__init__(message)


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


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


def template_body(
    *,
    customer_name: str,
    tech_name: str,
    reason: str,
    primary_quote_id: str,
    open_quotes: list[dict],
) -> str:
    listed = _quote_list(primary_quote_id, open_quotes)
    return (
        f"Hi {first_name(customer_name)}, it's {tech_name} following up. "
        f"Open quotes: {listed}. Reason: {reason}."
    )


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
    prior = bool(book or last_outbound)
    return {
        "prior_contact": prior,
        "last_outbound": last_outbound or book,
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
    prior = bool(contact.get("prior_contact"))
    if prior:
        touch = (
            "You already contacted this homeowner "
            f"(last outbound: {contact.get('last_outbound')}). "
            f"Last view: {contact.get('last_view')}. Last reply: {contact.get('last_reply')}. "
            "Acknowledge that prior contact. Do not write as if you have never spoken."
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
        raise DraftGenerationError() from exc


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
            raise DraftGenerationError() from exc
        stripped = "" if text is None else str(text).strip()
        if stripped and on_chunk:
            on_chunk(stripped)
        text = stripped
    if text is None or not str(text).strip():
        return fallback
    return str(text).strip()
