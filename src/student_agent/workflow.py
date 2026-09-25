from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from mcp.shared.exceptions import MCPError

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

MAX_TOOL_CALLS = 16
MAX_RETRIES = 2
TOOL_PERMISSIONS = {
    "entity-agent": {"get_order"},
    "customer-agent": {"get_customer_history"},
    "coordinator": {"get_order"},
    "order-agent": {"get_order_items", "get_sellers"},
    "product-agent": {"get_product_context"},
    "shipment-agent": {"get_shipment_summary"},
    "payment-agent": {"get_order_payments", "get_payment_timeline"},
    "refund-agent": {"get_refund_timeline"},
    "policy-agent": {"get_policy"},
}

async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Resolve one case, investigate its authoritative sources, and build its report."""
    case_id = str(case["case_id"])
    request = case.get("customer_request", {})
    scope = case.get("investigation_scope", {})
    evidence_refs: list[str] = []
    evidence: dict[str, dict[str, Any]] = {}
    cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    call_count = 0

    async def call(actor: str, tool_name: str, **arguments: str) -> dict[str, Any] | None:
        nonlocal call_count
        allowed_tools = TOOL_PERMISSIONS.get(actor, set())
        if tool_name not in allowed_tools:
            raise ValueError(f"{actor} is not permitted to call {tool_name}")
        cache_key = (tool_name, tuple(sorted(arguments.items())))
        if cache_key in cache:
            return cache[cache_key]
        if call_count >= MAX_TOOL_CALLS:
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="query_budget_exhausted",
                attributes={"tool_name": tool_name},
            )
            return None
        result: dict[str, Any] | None = None
        for attempt in range(MAX_RETRIES + 1):
            call_count += 1
            try:
                result = await gateway.call(tool_name, case_id=case_id, **arguments)
                break
            except (MCPError, RuntimeError, TimeoutError, OSError):
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.2 * (2**attempt))
            except ValueError:
                break
        if result is None:
            trace.emit(
                case_id=case_id,
                event_type="handoff",
                actor=actor,
                target="coordinator",
                decision_code="evidence_unavailable",
                attributes={"tool_name": tool_name, "attempts": min(attempt + 1, MAX_RETRIES + 1)},
            )
            return None
        cache[cache_key] = result
        reference = result["evidence_ref"]
        evidence_refs.append(reference)
        evidence[tool_name] = result
        trace.emit(
            case_id=case_id,
            event_type="tool_result_consumed",
            actor=actor,
            tool_name=tool_name,
            evidence_refs=[reference],
        )
        return result

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        attributes={"candidate_count": len(case.get("candidate_order_ids", []))},
    )

    policy_result = await call(
        "policy-agent", "get_policy", policy_version=str(case.get("policy_version", ""))
    )
    policy = _mapping(policy_result.get("data") if policy_result else None)
    rules = _mapping(policy.get("rules"))

    candidates = _unique_strings(
        [
            *case.get("candidate_order_ids", []),
            request.get("claimed_order_id"),
        ]
    )
    resolved_orders: list[dict[str, Any]] = []
    for candidate in candidates:
        result = await call("entity-agent", "get_order", order_id=candidate)
        if result and isinstance(result.get("data"), dict):
            order = result["data"]
            if order.get("order_id") == candidate:
                resolved_orders.append(order)

    claimed_order_id = request.get("claimed_order_id")
    selected_order = next(
        (order for order in resolved_orders if order.get("order_id") == claimed_order_id),
        resolved_orders[0] if resolved_orders else None,
    )
    resolved_order_id = str(selected_order["order_id"]) if selected_order else None
    rejected_candidates = [
        candidate for candidate in candidates if candidate != resolved_order_id
    ]
    resolution_status = (
        "not_found"
        if not resolved_orders
        else "resolved"
        if len(resolved_orders) == 1
        else "ambiguous"
    )
    resolution_confidence = 0.95 if resolution_status == "resolved" else 0.35
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code=f"entity_{resolution_status}",
    )

    if selected_order is None:
        report = _empty_report(
            case_id,
            request,
            resolution_status,
            rejected_candidates,
            resolution_confidence,
            evidence_refs,
        )
        _verify_report(report, evidence_refs)
        trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor="verifier",
            target="coordinator",
            evidence_refs=evidence_refs[:10],
            attributes={"evidence_count": len(evidence_refs)},
        )
        return report

    order_id = str(selected_order["order_id"])
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="specialists",
        attributes={"order_id": order_id},
    )
    order_items = await call("order-agent", "get_order_items", order_id=order_id)
    sellers = await call("order-agent", "get_sellers", order_id=order_id)
    product = (
        await call("product-agent", "get_product_context", order_id=order_id)
        if scope.get("include_product_context", True)
        else None
    )
    shipment = await call("shipment-agent", "get_shipment_summary", order_id=order_id)
    payments = await call("payment-agent", "get_order_payments", order_id=order_id)
    payment_timeline = await call(
        "payment-agent", "get_payment_timeline", order_id=order_id
    )
    refund_timeline = await call(
        "refund-agent", "get_refund_timeline", order_id=order_id
    )
    customer = (
        await call(
            "customer-agent",
            "get_customer_history",
            customer_unique_id=str(case.get("customer_unique_id_hint", "")),
        )
        if scope.get("include_customer_history", True)
        else None
    )

    shipment_data = _mapping(shipment.get("data") if shipment else None)
    payment_data = _mapping(payment_timeline.get("data") if payment_timeline else None)
    payment_rows = _list_of_mappings(payments.get("data") if payments else None)
    payment_events = _list_of_mappings(payment_data.get("events"))
    refund_data = _mapping(refund_timeline.get("data") if refund_timeline else None)
    refund_events = _list_of_mappings(refund_data.get("events"))
    item_rows = _list_of_mappings(order_items.get("data") if order_items else None)
    seller_rows = _list_of_mappings(sellers.get("data") if sellers else None)
    shipment_verdict, late_sellers, timeline_complete = _shipment_verdict(
        selected_order, shipment_data
    )
    payment_verdict, captured, refunded, refundable = _payment_verdict(
        payment_rows, payment_events, refund_events
    )
    primary_issue = _primary_issue(
        selected_order, shipment_verdict, payment_verdict, refund_events, request
    )
    rule = _mapping(rules.get(primary_issue))
    if not rule:
        rule = _mapping(rules.get("unsupported_claim"))
    case_status = str(rule.get("case_status", "needs_investigation"))
    recommended_refund = _number(rule.get("refund_brl"), 0.0)
    if primary_issue in {"valid_split_payment", "unsupported_claim"}:
        recommended_refund = 0.0
    if payment_verdict == "refund_pending" or payment_verdict == "refund_failed":
        recommended_refund = 0.0

    order_ids = [order_id]
    item_ids = _unique_strings([row.get("order_item_id") for row in item_rows])
    seller_ids = _unique_strings(
        [row.get("seller_id") for row in item_rows + seller_rows]
    )
    payment_references = _unique_strings(
        [
            f"payment-{order_id}-{row.get('payment_sequential', index)}"
            for index, row in enumerate(payment_rows, 1)
        ]
    )
    shipment_ids = [f"shipment-{order_id}"] if shipment_data else []
    customer_data = _mapping(customer.get("data") if customer else None)
    related_order_ids = _unique_strings(
        [row.get("order_id") for row in _list_of_mappings(customer_data.get("orders"))]
    )
    conflicts = _conflicts(selected_order, shipment_data, payment_rows, payment_events)
    secondary = _secondary_issues(primary_issue, shipment_verdict, payment_verdict)
    claim_assessments = [
        {
            "claim_id": str(claim.get("claim_id", "claim")),
            "verdict": _claim_verdict(
                str(claim.get("topic", "")), primary_issue, shipment_verdict, payment_verdict
            ),
            "confidence": 0.9 if evidence_refs else 0.3,
            "evidence_refs": evidence_refs[:4],
        }
        for claim in request.get("claims", [])
        if isinstance(claim, dict)
    ][:5]
    responsible = rule.get("responsible_parties")
    if not isinstance(responsible, list):
        responsible = [{"party_type": "unknown", "party_id": None}]
    actions = _actions(rule, primary_issue, payment_verdict)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="conflict-resolver",
        decision_code=primary_issue,
        evidence_refs=evidence_refs[:10],
    )
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        target="coordinator",
        evidence_refs=evidence_refs[:10],
        attributes={"evidence_count": len(evidence_refs)},
    )
    report = {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "secondary_issues": secondary,
            "case_status": case_status,
            "confidence": 0.9 if resolution_status == "resolved" else 0.35,
        },
        "affected_entities": {
            "order_ids": order_ids,
            "item_ids": item_ids,
            "seller_ids": seller_ids,
            "payment_references": payment_references,
            "shipment_ids": shipment_ids,
        },
        "claim_assessments": claim_assessments,
        "entity_resolution": {
            "status": resolution_status,
            "resolved_order_ids": order_ids,
            "rejected_candidates": rejected_candidates,
            "confidence": resolution_confidence,
        },
        "customer_context": {
            "customer_unique_id": case.get("customer_unique_id_hint"),
            "related_order_ids": related_order_ids,
        },
        "shipment_analysis": {
            "verdict": shipment_verdict,
            "late_seller_ids": late_sellers,
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment_verdict,
            "captured_total_brl": captured,
            "refunded_total_brl": refunded,
            "refundable_total_brl": refundable,
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}],
            "responsible_parties": responsible[:5],
        },
        "evidence_refs": _unique_strings(evidence_refs)[:30],
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": recommended_refund,
            "refund_lines": (
                [{"reason_code": primary_issue, "amount_brl": recommended_refund, "entity_id": order_id}]
                if recommended_refund > 0
                else []
            ),
        },
        "resolution_actions": actions,
    }
    _verify_report(report, evidence_refs)
    return report


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_of_mappings(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _unique_strings(values: list[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value is not None and str(value) and str(value) not in result:
            result.append(str(value))
    return result[:20]


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _date(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _shipment_verdict(order: dict[str, Any], shipment: dict[str, Any]) -> tuple[str, list[str], bool]:
    if not shipment:
        return "insufficient_evidence", [], False
    status = str(shipment.get("order_status", order.get("order_status", ""))).lower()
    events = _list_of_mappings(shipment.get("events"))
    if status in {"lost", "unavailable"}:
        return "lost", [], bool(events)
    if status == "returned":
        return "returned", [], bool(events)
    late_events = [event for event in events if "late" in str(event.get("event_type", "")).lower()]
    if late_events:
        actors = {str(event.get("actor", "")).lower() for event in late_events}
        if "seller" in actors:
            return "seller_delay", _unique_strings([row.get("seller_id") for row in shipment.get("shipping_limits", [])]), True
        return "logistics_delay", [], True
    delivered = _date(shipment.get("delivered_customer_at"))
    estimated = _date(shipment.get("estimated_delivery_at"))
    if delivered and estimated and delivered > estimated:
        return "logistics_delay", [], True
    return "on_time", [], bool(delivered and estimated)


def _payment_verdict(
    rows: list[dict[str, Any]], events: list[dict[str, Any]], refunds: list[dict[str, Any]]
) -> tuple[str, float | None, float | None, float | None]:
    base_total = sum(_number(row.get("payment_value"), 0.0) or 0.0 for row in rows)
    captured_events = [event for event in events if str(event.get("event_type", "")).lower() == "captured"]
    captured = sum(_number(event.get("amount_brl"), 0.0) or 0.0 for event in captured_events)
    if not captured_events and rows:
        captured = base_total
    if rows and captured and abs(captured - base_total) > 0.01:
        verdict = "capture_mismatch"
    elif len(captured_events) > len(rows) and captured > base_total:
        verdict = "duplicate_capture"
    else:
        verdict = "reconciled" if rows or captured_events else "insufficient_evidence"
    refunded = sum(
        _number(event.get("amount_brl", event.get("refund_amount_brl")), 0.0) or 0.0
        for event in refunds
        if str(event.get("status", event.get("event_type", ""))).lower() in {"confirmed", "refunded", "succeeded", "completed"}
    )
    pending = any(str(event.get("status", "")).lower() == "pending" for event in refunds)
    failed = any(str(event.get("status", "")).lower() == "failed" for event in refunds)
    if failed:
        verdict = "refund_failed"
    elif pending:
        verdict = "refund_pending"
    elif refunded > 0:
        verdict = "refunded"
    return verdict, round(captured, 2) if rows or captured_events else None, round(refunded, 2), round(max(captured - refunded, 0.0), 2) if rows or captured_events else None


def _primary_issue(
    order: dict[str, Any], shipment: str, payment: str, refunds: list[dict[str, Any]], request: dict[str, Any]
) -> str:
    if payment in {"refund_pending", "refund_failed"}:
        return payment
    if payment == "duplicate_capture":
        return "duplicate_charge"
    if payment == "capture_mismatch":
        return "payment_mismatch"
    if shipment == "seller_delay":
        return "late_delivery_seller"
    if shipment == "logistics_delay":
        return "late_delivery_logistics"
    status = str(order.get("order_status", "")).lower()
    if status == "canceled" and payment == "reconciled":
        return "canceled_order_paid"
    topics = [str(claim.get("topic", "")) for claim in request.get("claims", []) if isinstance(claim, dict)]
    for topic in topics:
        if topic in {"valid_split_payment", "unsupported_claim"}:
            return topic
    return "insufficient_evidence"


def _claim_verdict(topic: str, primary: str, shipment: str, payment: str) -> str:
    if topic == primary or topic == shipment or topic == payment:
        return "supported"
    if topic == "requested_full_refund":
        return "partially_supported" if primary not in {"unsupported_claim", "valid_split_payment"} else "unsupported"
    return "insufficient_evidence"


def _secondary_issues(primary: str, shipment: str, payment: str) -> list[str]:
    values = []
    for value in (shipment, payment):
        if value not in {primary, "on_time", "reconciled", "insufficient_evidence"} and value not in values:
            values.append(value)
    return values


def _actions(rule: dict[str, Any], primary: str, payment: str) -> list[str]:
    action = rule.get("recommended_action")
    if isinstance(action, str) and action:
        return [action]
    if payment == "refund_pending":
        return ["monitor_refund"]
    return ["document_no_action"] if primary in {"valid_split_payment", "unsupported_claim"} else ["investigate_case"]


def _conflicts(
    order: dict[str, Any], shipment: dict[str, Any], rows: list[dict[str, Any]], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    if order.get("order_status") and shipment.get("order_status") and order["order_status"] != shipment["order_status"]:
        conflicts.append({"field": "order_status", "sources": ["order", "shipment"], "selected_source": "order", "resolution_code": "authoritative_order"})
    if rows and events and abs(sum(_number(row.get("payment_value"), 0.0) or 0.0 for row in rows) - sum(_number(event.get("amount_brl"), 0.0) or 0.0 for event in events if event.get("event_type") == "captured")) > 0.01:
        conflicts.append({"field": "payment_total", "sources": ["order_payments", "payment_timeline"], "selected_source": "payment_timeline", "resolution_code": "authoritative_lifecycle"})
    return conflicts[:5]


def _verify_report(report: dict[str, Any], evidence_refs: list[str]) -> None:
    """Check cross-field invariants before the public schema validator runs."""
    known_refs = set(evidence_refs)
    submitted_refs = set(report.get("evidence_refs", []))
    if not submitted_refs.issubset(known_refs):
        raise ValueError("report contains evidence outside the current case")

    resolution = _mapping(report.get("entity_resolution"))
    affected = _mapping(report.get("affected_entities"))
    resolved_ids = set(resolution.get("resolved_order_ids", []))
    affected_orders = set(affected.get("order_ids", []))
    rejected_ids = set(resolution.get("rejected_candidates", []))
    if not resolved_ids.issubset(affected_orders):
        raise ValueError("resolved orders must be affected entities")
    if resolved_ids & rejected_ids:
        raise ValueError("an order cannot be both resolved and rejected")

    assessment = _mapping(report.get("assessment"))
    confidence = _number(assessment.get("confidence"))
    if confidence is None or not 0 <= confidence <= 1:
        raise ValueError("assessment confidence is outside [0, 1]")
    resolution_confidence = _number(resolution.get("confidence"))
    if resolution_confidence is None or not 0 <= resolution_confidence <= 1:
        raise ValueError("entity resolution confidence is outside [0, 1]")

    for claim in report.get("claim_assessments", []):
        claim_refs = set(claim.get("evidence_refs", []))
        claim_confidence = _number(claim.get("confidence"))
        if not claim_refs.issubset(known_refs):
            raise ValueError("claim contains evidence outside the current case")
        if claim_confidence is None or not 0 <= claim_confidence <= 1:
            raise ValueError("claim confidence is outside [0, 1]")

    payment = _mapping(report.get("payment_analysis"))
    captured = _number(payment.get("captured_total_brl"))
    refunded = _number(payment.get("refunded_total_brl"))
    refundable = _number(payment.get("refundable_total_brl"))
    if any(value is not None and value < 0 for value in (captured, refunded, refundable)):
        raise ValueError("payment totals cannot be negative")
    if captured is not None and refunded is not None and refundable is not None:
        if abs(refundable - max(captured - refunded, 0.0)) > 0.01:
            raise ValueError("refundable total does not reconcile")

    financial = _mapping(report.get("financial_resolution"))
    refund_lines = _list_of_mappings(financial.get("refund_lines"))
    recommended = _number(financial.get("recommended_refund_brl"), 0.0) or 0.0
    line_total = sum(_number(line.get("amount_brl"), 0.0) or 0.0 for line in refund_lines)
    if abs(recommended - line_total) > 0.01:
        raise ValueError("refund lines do not reconcile with recommended refund")
    if assessment.get("case_status") == "no_action" and recommended > 0:
        raise ValueError("no-action case cannot recommend a refund")
    if not report.get("resolution_actions"):
        raise ValueError("final report must contain a resolution action")


def _empty_report(
    case_id: str,
    request: dict[str, Any],
    status: str,
    rejected: list[str],
    confidence: float,
    evidence_refs: list[str],
) -> dict[str, Any]:
    claims = [
        {"claim_id": str(claim.get("claim_id", "claim")), "verdict": "insufficient_evidence", "confidence": confidence, "evidence_refs": evidence_refs[:4]}
        for claim in request.get("claims", [])
        if isinstance(claim, dict)
    ][:5]
    return {
        "schema_version": "day09-l3b-output-v2",
        "case_id": case_id,
        "assessment": {"primary_issue": "insufficient_evidence", "secondary_issues": [], "case_status": "needs_investigation", "confidence": confidence},
        "affected_entities": {"order_ids": [], "item_ids": [], "seller_ids": [], "payment_references": [], "shipment_ids": []},
        "claim_assessments": claims,
        "entity_resolution": {"status": status, "resolved_order_ids": [], "rejected_candidates": rejected, "confidence": confidence},
        "customer_context": {"customer_unique_id": None, "related_order_ids": []},
        "shipment_analysis": {"verdict": "insufficient_evidence", "late_seller_ids": [], "timeline_complete": False},
        "payment_analysis": {"verdict": "insufficient_evidence", "captured_total_brl": None, "refunded_total_brl": None, "refundable_total_brl": None},
        "root_cause_analysis": {"ranked_causes": [{"cause_code": "INSUFFICIENT_EVIDENCE", "rank": 1}], "responsible_parties": [{"party_type": "unknown", "party_id": None}]},
        "evidence_refs": _unique_strings(evidence_refs),
        "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0.0, "refund_lines": []},
        "resolution_actions": ["investigate_case"],
    }
