"""Port of external-dns v0.20's in-tree DigitalOcean provider (with bug fixes)."""

from typing import Any, Protocol

import structlog
from opentelemetry import metrics

log = structlog.get_logger(__name__)
changes_counter = metrics.get_meter(__name__).create_counter(
    "dns.record.changes", unit="{change}", description="DNS record changes applied"
)


def _count(action: str, record_type: str, zone: str) -> None:
    changes_counter.add(1, {"action": action, "record_type": record_type, "zone": zone})


DEFAULT_TTL = 300
SUPPORTED_TYPES = {"A", "AAAA", "CNAME", "TXT", "NS", "MX"}
DOTTED_TYPES = {"CNAME", "MX", "NS"}  # DO returns these with a trailing dot

Endpoint = dict[str, Any]


class Client(Protocol):
    def list_zones(self) -> list[str]: ...
    def list_records(self, zone: str) -> list[dict]: ...
    def create_record(self, zone: str, body: dict) -> None: ...
    def edit_record(self, zone: str, record_id: int, body: dict) -> None: ...
    def delete_record(self, zone: str, record_id: int) -> None: ...


def _norm_name(name: str) -> str:
    return name.lower().rstrip(".")


def _in_zone(name: str, zone: str) -> bool:
    name, zone = _norm_name(name), _norm_name(zone)
    return name == zone or name.endswith("." + zone)


def _record_target(r: dict) -> str:
    """Target string as external-dns sees it for a DO record."""
    data = r["data"]
    if r["type"] in DOTTED_TYPES:
        data = data.removesuffix(".")
    if r["type"] == "MX":
        data = f"{r.get('priority') or 0} {data}"
    return data


def _target_key(record_type: str, target: str) -> str:
    target = target.lower()
    return target.removesuffix(".") if record_type in DOTTED_TYPES else target


def _record_name(r: dict, zone: str) -> str:
    return zone if r["name"] == "@" else f"{r['name']}.{zone}"


def _edit_body(zone: str, dns_name: str, record_type: str, target: str, ttl: int) -> dict:
    name = dns_name.removesuffix("." + zone)
    if name == zone:
        name = "@"
    if record_type in ("CNAME", "MX") and not target.endswith("."):
        target += "."
    body: dict[str, Any] = {"type": record_type, "name": name, "data": target, "ttl": ttl}
    if record_type == "MX":
        try:
            prio, host = target.split(None, 1)
            body["priority"] = int(prio)
            body["data"] = host if host.endswith(".") else host + "."
        except ValueError:
            log.warning("unparseable_mx_target", target=target, dns_name=dns_name)
    return body


def _matching(records: list[dict], zone: str, ep: Endpoint) -> list[dict]:
    name = "@" if ep["dnsName"] == zone else ep["dnsName"].removesuffix("." + zone)
    return [r for r in records if r["name"] == name and r["type"] == ep["recordType"]]


class Provider:
    def __init__(self, client: Client, domain_filter: list[str]):
        self.client = client
        self.filter = domain_filter

    def domain_filter(self) -> list[str]:
        return self.filter

    def _zones(self) -> list[str]:
        filters = [_norm_name(f) for f in self.filter]
        return [
            z
            for z in self.client.list_zones()
            if not filters or any(_in_zone(z, f) for f in filters)
        ]

    def records(self) -> list[Endpoint]:
        merged: dict[tuple[str, str], Endpoint] = {}
        for zone in self._zones():
            for r in self.client.list_records(zone):
                if r["type"] not in SUPPORTED_TYPES:
                    continue
                name = _record_name(r, zone)
                e = merged.setdefault(
                    (name, r["type"]),
                    {"dnsName": name, "targets": [], "recordType": r["type"]},
                )
                if r.get("ttl") and "recordTTL" not in e:
                    e["recordTTL"] = r["ttl"]
                e["targets"].append(_record_target(r))
        return list(merged.values())

    def apply(self, changes: dict) -> None:
        # external-dns sends lowercase keys (create, updateNew, ...); Go decodes case-insensitively.
        changes = {k.lower(): v for k, v in changes.items()}
        zones = self._zones()
        records = {z: self.client.list_records(z) for z in zones}

        def by_zone(eps: list[Endpoint] | None) -> list[tuple[str, Endpoint]]:
            out = []
            for ep in eps or []:
                cands = [z for z in zones if _in_zone(ep["dnsName"], z)]
                if not cands:
                    log.info("skip_no_matching_zone", dns_name=ep["dnsName"])
                    continue
                out.append((max(cands, key=len), ep))
            return out

        creates: list[tuple[str, dict]] = []
        updates: list[tuple[str, int, dict]] = []
        deletes: list[tuple[str, dict]] = []

        for zone, ep in by_zone(changes.get("create")):
            ttl = ep.get("recordTTL") or DEFAULT_TTL
            for t in ep.get("targets") or []:
                creates.append((zone, _edit_body(zone, ep["dnsName"], ep["recordType"], t, ttl)))

        for zone, ep in by_zone(changes.get("updatenew")):
            ttl = ep.get("recordTTL") or DEFAULT_TTL
            rtype = ep["recordType"]
            existing = _matching(records[zone], zone, ep)
            by_target: dict[str, dict] = {}
            for r in existing:
                by_target.setdefault(_target_key(rtype, _record_target(r)), r)
            for t in ep.get("targets") or []:
                body = _edit_body(zone, ep["dnsName"], rtype, t, ttl)
                r = by_target.pop(_target_key(rtype, t), None)
                if r:
                    updates.append((zone, r["id"], body))
                else:
                    creates.append((zone, body))
            deletes.extend((zone, r) for r in by_target.values())

        for zone, ep in by_zone(changes.get("delete")):
            rtype = ep["recordType"]
            keys = {_target_key(rtype, t) for t in ep.get("targets") or []}
            deletes.extend(
                (zone, r)
                for r in _matching(records[zone], zone, ep)
                if _target_key(rtype, _record_target(r)) in keys
            )

        for zone, body in creates:
            log.info(
                "create_record", type=body["type"], name=body["name"], data=body["data"], zone=zone
            )
            self.client.create_record(zone, body)
            _count("create", body["type"], zone)
        for zone, rid, body in updates:
            log.info(
                "updat_record", type=body["type"], name=body["name"], data=body["data"], zone=zone
            )
            self.client.edit_record(zone, rid, body)
            _count("update", body["type"], zone)
        for zone, r in deletes:
            log.info("delet_record", type=r["type"], name=r["name"], data=r["data"], zone=zone)
            self.client.delete_record(zone, r["id"])
            _count("delete", r["type"], zone)
