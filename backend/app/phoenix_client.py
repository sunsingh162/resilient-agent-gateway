"""Phase 4, Step 6: minimal client for Phoenix's REST API.

Only what the judge (Step 6) and monitoring daemon (Step 7) need: read
spans back out of the project the gateway exports to, and write
evaluation scores back onto them as span annotations so they show up next
to the trace in the Phoenix UI. Plain httpx rather than the arize-phoenix
client package, to keep the read path obvious and easy to fake in tests.
"""

import httpx

from .tracing import PHOENIX_COLLECTOR_ENDPOINT, PHOENIX_PROJECT_NAME

_PAGE_LIMIT = 1000  # Phoenix's maximum page size for /spans


def get_spans(
    *,
    name: str | None = None,
    trace_id: str | None = None,
    span_kind: str | None = None,
    start_time: str | None = None,
    limit: int | None = None,
    base_url: str = PHOENIX_COLLECTOR_ENDPOINT,
    project: str = PHOENIX_PROJECT_NAME,
) -> list[dict]:
    """Newest first. Follows Phoenix's cursor pagination until `limit`
    spans are collected or there are no more pages. `start_time` (ISO 8601)
    keeps only spans that started at or after it — the monitoring daemon's
    rolling window (Step 7)."""
    params: dict = {"sort": "start_time", "order": "desc"}
    if name:
        params["name"] = name
    if trace_id:
        params["trace_id"] = trace_id
    if span_kind:
        params["span_kind"] = span_kind
    if start_time:
        params["start_time"] = start_time

    spans: list[dict] = []
    cursor = None
    with httpx.Client(base_url=base_url, timeout=10) as client:
        while True:
            page_limit = _PAGE_LIMIT if limit is None else min(_PAGE_LIMIT, limit - len(spans))
            resp = client.get(
                f"/v1/projects/{project}/spans",
                params={**params, "limit": page_limit, **({"cursor": cursor} if cursor else {})},
            )
            resp.raise_for_status()
            body = resp.json()
            spans.extend(body["data"])
            cursor = body.get("next_cursor")
            if not cursor or (limit is not None and len(spans) >= limit):
                return spans


def annotate_spans(annotations: list[dict], base_url: str = PHOENIX_COLLECTOR_ENDPOINT) -> None:
    """Each item: {span_id, name, annotator_kind, result: {label, score, explanation}}."""
    if not annotations:
        return
    with httpx.Client(base_url=base_url, timeout=10) as client:
        resp = client.post("/v1/span_annotations", params={"sync": "true"}, json={"data": annotations})
        resp.raise_for_status()
