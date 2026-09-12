"""Item-aware Waterlog telemetry uploader."""

from __future__ import annotations

import json
import logging
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable

from .http import HttpTransport, TransportError
from .models import BridgeConfig, QueueItem, UploadOutcome
from .queue import DurableQueue


LOGGER = logging.getLogger(__name__)
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


class AcknowledgementProtocolError(ValueError):
    pass


class WaterlogUploader:
    def __init__(
        self,
        config: BridgeConfig,
        queue: DurableQueue,
        *,
        transport: HttpTransport | None = None,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self._config = config
        self._queue = queue
        self._transport = transport or HttpTransport()
        self._jitter = jitter

    def upload_once(self, *, now: float) -> UploadOutcome:
        if self._config.credential is None:
            return UploadOutcome()
        items = self._queue.due_items(now=now, limit=self._config.batch_size)
        if not items:
            return UploadOutcome()

        samples = [item.payload for item in items if item.kind == "sample"]
        statuses = [item.payload for item in items if item.kind == "status"]
        try:
            response = self._transport.post_json(
                self._config.ingest_url,
                headers={
                    "Authorization": f"Bearer {self._config.credential}",
                    "Accept": "application/json",
                    "User-Agent": "Waterlog-Home-Assistant-Bridge/0.1",
                },
                payload={"samples": samples, "statuses": statuses},
                timeout=self._config.request_timeout_seconds,
            )
        except TransportError:
            retry_at = self._retry(items, now=now, error_code="network_error")
            LOGGER.warning(
                "Telemetry upload failed before an HTTP response; retry scheduled"
            )
            return UploadOutcome(
                attempted=len(items), retryable=len(items), retry_at=retry_at
            )

        if response.status in {401, 403}:
            LOGGER.critical(
                "Waterlog rejected the bridge credential (HTTP %s); uploads are disabled until the app is restarted after credential repair",
                response.status,
            )
            return UploadOutcome(
                attempted=len(items), retryable=len(items), auth_blocked=True
            )

        if response.status == 429:
            retry_after = _parse_retry_after(response.headers.get("retry-after"), now)
            retry_at = self._retry(
                items,
                now=now,
                error_code="rate_limited",
                minimum_delay=retry_after,
            )
            LOGGER.warning("Waterlog rate-limited telemetry; retry scheduled")
            return UploadOutcome(
                attempted=len(items), retryable=len(items), retry_at=retry_at
            )

        if response.status >= 500 or response.status in {408, 425}:
            retry_at = self._retry(items, now=now, error_code=f"http_{response.status}")
            LOGGER.warning(
                "Waterlog telemetry endpoint returned HTTP %s; retry scheduled",
                response.status,
            )
            return UploadOutcome(
                attempted=len(items), retryable=len(items), retry_at=retry_at
            )

        try:
            body = json.loads(response.body)
            decisions = parse_acknowledgements(body, items)
        except (json.JSONDecodeError, UnicodeDecodeError, AcknowledgementProtocolError):
            if response.status in {400, 409, 422}:
                reason = f"unstructured_http_{response.status}"
                self._queue.quarantine(
                    [item.client_id for item in items], reason=reason, now=now
                )
                LOGGER.error(
                    "Waterlog permanently rejected an unstructured batch (HTTP %s); %s items quarantined",
                    response.status,
                    len(items),
                )
                return UploadOutcome(
                    attempted=len(items),
                    quarantined=len(items),
                    has_more_due=self._queue.has_due(now=now),
                )
            retry_at = self._retry(items, now=now, error_code="invalid_acknowledgement")
            LOGGER.error(
                "Waterlog returned an invalid item acknowledgement; retry scheduled"
            )
            return UploadOutcome(
                attempted=len(items), retryable=len(items), retry_at=retry_at
            )

        if not 200 <= response.status < 300 and not decisions:
            retry_at = self._retry(items, now=now, error_code=f"http_{response.status}")
            LOGGER.error(
                "Waterlog telemetry endpoint returned HTTP %s without item results; retry scheduled",
                response.status,
            )
            return UploadOutcome(
                attempted=len(items), retryable=len(items), retry_at=retry_at
            )

        accepted: list[str] = []
        retryable: list[QueueItem] = []
        quarantined = 0
        for item in items:
            decision = decisions.get(item.client_id)
            if decision is None or decision[0] == "retryable":
                retryable.append(item)
            elif decision[0] in {"accepted", "duplicate"}:
                accepted.append(item.client_id)
            elif decision[0] in {"rejected", "conflict"}:
                reason = _safe_code(decision[1], decision[0])
                self._queue.quarantine([item.client_id], reason=reason, now=now)
                quarantined += 1
                LOGGER.error(
                    "Telemetry item %s was permanently rejected (%s) and quarantined",
                    item.client_id,
                    reason,
                )
            else:  # Parser currently prevents this; preserve data if extended badly.
                retryable.append(item)

        self._queue.acknowledge(accepted)
        retry_at: float | None = None
        if retryable:
            retry_at = self._retry(
                retryable, now=now, error_code="missing_or_retryable_ack"
            )
        has_more = self._queue.has_due(now=now)
        if accepted:
            LOGGER.info(
                "Waterlog acknowledged %s queued telemetry items", len(accepted)
            )
        return UploadOutcome(
            attempted=len(items),
            acknowledged=len(accepted),
            quarantined=quarantined,
            retryable=len(retryable),
            retry_at=retry_at,
            has_more_due=has_more,
        )

    def _retry(
        self,
        items: list[QueueItem],
        *,
        now: float,
        error_code: str,
        minimum_delay: float | None = None,
    ) -> float:
        highest_attempt = max((item.attempt_count for item in items), default=0)
        exponential = min(1800.0, 30.0 * (2 ** min(highest_attempt, 6)))
        delay = self._jitter(exponential * 0.8, exponential * 1.2)
        if minimum_delay is not None:
            delay = max(delay, minimum_delay)
        cap = 86_400.0 if minimum_delay is not None else 7200.0
        retry_at = now + min(cap, max(1.0, delay))
        self._queue.retry(
            [item.client_id for item in items],
            error_code=error_code,
            next_attempt_at=retry_at,
        )
        return retry_at


def parse_acknowledgements(
    payload: object, items: list[QueueItem]
) -> dict[str, tuple[str, str | None]]:
    """Accept indexed arrays and a defensive unified-results alternative."""

    if not isinstance(payload, dict):
        raise AcknowledgementProtocolError("response must be an object")
    by_kind = {
        "sample": [item for item in items if item.kind == "sample"],
        "status": [item for item in items if item.kind == "status"],
    }
    by_id = {item.client_id: item for item in items}
    decisions: dict[str, tuple[str, str | None]] = {}

    found_shape = False
    for response_key, kind in (("samples", "sample"), ("statuses", "status")):
        if response_key not in payload:
            continue
        found_shape = True
        results = payload[response_key]
        if not isinstance(results, list):
            raise AcknowledgementProtocolError(f"{response_key} must be an array")
        for result in results:
            item = _resolve_result(result, kind, by_kind, by_id)
            _record_decision(decisions, item.client_id, result)

    if "results" in payload:
        found_shape = True
        results = payload["results"]
        if not isinstance(results, list):
            raise AcknowledgementProtocolError("results must be an array")
        for result in results:
            if not isinstance(result, dict):
                raise AcknowledgementProtocolError("result must be an object")
            raw_kind = result.get("kind") or result.get("itemType")
            kind = (
                {"samples": "sample", "statuses": "status"}.get(raw_kind, raw_kind)
                if isinstance(raw_kind, str)
                else None
            )
            if kind not in {"sample", "status"}:
                if "clientSampleId" in result:
                    kind = "sample"
                elif "clientStatusId" in result:
                    kind = "status"
                else:
                    raise AcknowledgementProtocolError("unified result needs a kind")
            item = _resolve_result(result, kind, by_kind, by_id)
            _record_decision(decisions, item.client_id, result)

    if not found_shape:
        raise AcknowledgementProtocolError("response has no item results")
    return decisions


def _resolve_result(
    result: object,
    kind: str,
    by_kind: dict[str, list[QueueItem]],
    by_id: dict[str, QueueItem],
) -> QueueItem:
    if not isinstance(result, dict):
        raise AcknowledgementProtocolError("result must be an object")
    client_id = (
        result.get("clientSampleId")
        or result.get("clientStatusId")
        or result.get("clientId")
    )
    item: QueueItem | None = None
    if isinstance(client_id, str):
        item = by_id.get(client_id)
    index = result.get("index")
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, int):
            raise AcknowledgementProtocolError("result index must be an integer")
        candidates = by_kind[kind]
        if index < 0 or index >= len(candidates):
            raise AcknowledgementProtocolError("result index is out of range")
        indexed_item = candidates[index]
        if item is not None and item.client_id != indexed_item.client_id:
            raise AcknowledgementProtocolError("result ID and index disagree")
        item = indexed_item
    if item is None or item.kind != kind:
        raise AcknowledgementProtocolError("result does not identify a batch item")
    return item


def _record_decision(
    decisions: dict[str, tuple[str, str | None]], client_id: str, result: object
) -> None:
    assert isinstance(result, dict)
    status = result.get("status")
    if status not in {"accepted", "duplicate", "rejected", "conflict", "retryable"}:
        raise AcknowledgementProtocolError("result has an unknown status")
    code = result.get("code")
    code = code if isinstance(code, str) else None
    decision = (status, code)
    if client_id in decisions and decisions[client_id] != decision:
        raise AcknowledgementProtocolError("conflicting duplicate results")
    decisions[client_id] = decision


def _safe_code(code: str | None, fallback: str) -> str:
    return code if code and _SAFE_CODE.fullmatch(code) else fallback


def _parse_retry_after(value: str | None, now: float) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value.strip())
        return min(86_400.0, max(1.0, seconds))
    except ValueError:
        pass
    try:
        retry_datetime = parsedate_to_datetime(value)
        if retry_datetime.tzinfo is None:
            retry_datetime = retry_datetime.replace(tzinfo=timezone.utc)
        now_datetime = datetime.fromtimestamp(now, tz=timezone.utc)
        return min(
            86_400.0,
            max(1.0, (retry_datetime - now_datetime).total_seconds()),
        )
    except (TypeError, ValueError, OverflowError):
        return None
