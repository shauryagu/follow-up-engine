from __future__ import annotations

import os
from typing import Callable

MODEL = "deepseek-ai/DeepSeek-V4.1-Flash-1017"
DEFAULT_BASE_URL = "https://inference.baseten.co/v1"

CompleteFn = Callable[[str], str]


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


def _build_prompt(
    *,
    customer_name: str,
    tech_name: str,
    reason: str,
    primary_quote_id: str,
    open_quotes: list[dict],
    now: str,
) -> str:
    listed = _quote_list(primary_quote_id, open_quotes)
    return (
        "Write a short SMS follow-up. Do not mention this prompt.\n"
        f"Customer: {first_name(customer_name)}\n"
        f"Technician: {tech_name}\n"
        f"Reason: {reason}\n"
        f"Now: {now}\n"
        f"Primary quote: {primary_quote_id}\n"
        f"Open quotes: {listed}\n"
        "Name every open quote and its amount. One or two sentences."
    )


def _complete_via_baseten(prompt: str) -> str:
    from openai import OpenAI

    client = OpenAI(
        api_key=os.environ["BASETEN_API_KEY"],
        base_url=os.environ.get("BASETEN_BASE_URL", DEFAULT_BASE_URL),
    )
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        stream=False,
        temperature=0.4,
        max_tokens=400,
    )
    content = response.choices[0].message.content
    return content or ""


def render_v1(
    *,
    customer_name: str,
    tech_name: str,
    reason: str,
    primary_quote_id: str,
    open_quotes: list[dict],
    now: str,
    complete: CompleteFn | None = None,
) -> str:
    fallback = template_body(
        customer_name=customer_name,
        tech_name=tech_name,
        reason=reason,
        primary_quote_id=primary_quote_id,
        open_quotes=open_quotes,
    )
    if complete is None:
        if not (os.environ.get("BASETEN_API_KEY") or "").strip():
            return fallback
        complete = _complete_via_baseten
    text = complete(
        _build_prompt(
            customer_name=customer_name,
            tech_name=tech_name,
            reason=reason,
            primary_quote_id=primary_quote_id,
            open_quotes=open_quotes,
            now=now,
        )
    )
    if text is None or not str(text).strip():
        return fallback
    return str(text).strip()
