import pytest

from external_dns_digitalocean.provider import Provider


class FakeClient:
    def __init__(self, zones: dict[str, list[dict]]):
        self.zones = zones
        self.calls: list[tuple] = []

    def list_zones(self):
        return list(self.zones)

    def list_records(self, zone):
        return list(self.zones[zone])

    def create_record(self, zone, body):
        self.calls.append(("create", zone, body))

    def edit_record(self, zone, record_id, body):
        self.calls.append(("edit", zone, record_id, body))

    def delete_record(self, zone, record_id):
        self.calls.append(("delete", zone, record_id))


def rec(id, type, name, data, ttl=300, priority=None):
    return {"id": id, "type": type, "name": name, "data": data, "ttl": ttl, "priority": priority}


def ep(name, type, *targets, ttl=0):
    d = {"dnsName": name, "recordType": type, "targets": list(targets)}
    if ttl:
        d["recordTTL"] = ttl
    return d


def provider(zones, filters=()):
    c = FakeClient(zones)
    return Provider(c, list(filters)), c


# --- reading -------------------------------------------------------------


def test_records_conversion():
    p, _ = provider(
        {
            "example.com": [
                rec(1, "A", "@", "1.2.3.4", 60),
                rec(2, "A", "www", "1.2.3.4", 120),
                rec(3, "MX", "@", "mail.example.com.", 300, 10),
                rec(4, "CNAME", "blog", "Target.Example.com.", 300),
                rec(5, "NS", "@", "ns1.digitalocean.com.", 1800),
                rec(6, "TXT", "x", "hello", 300),
                rec(7, "AAAA", "v6", "::1", 300),
            ]
        }
    )
    assert p.records() == [
        ep("example.com", "A", "1.2.3.4", ttl=60),
        ep("www.example.com", "A", "1.2.3.4", ttl=120),
        ep("example.com", "MX", "10 mail.example.com", ttl=300),
        ep("blog.example.com", "CNAME", "Target.Example.com", ttl=300),
        ep("example.com", "NS", "ns1.digitalocean.com", ttl=1800),
        ep("x.example.com", "TXT", "hello", ttl=300),
        ep("v6.example.com", "AAAA", "::1", ttl=300),
    ]


def test_records_merge_keeps_ttl():
    p, _ = provider(
        {"example.com": [rec(1, "A", "w", "1.1.1.1", 77), rec(2, "A", "w", "2.2.2.2", 77)]}
    )
    assert p.records() == [ep("w.example.com", "A", "1.1.1.1", "2.2.2.2", ttl=77)]


def test_records_drops_unsupported_types():
    p, _ = provider(
        {
            "example.com": [
                rec(1, "SRV", "_s._tcp", "x"),
                rec(2, "SOA", "@", "x"),
                rec(3, "CAA", "@", "x"),
            ]
        }
    )
    assert p.records() == []


def test_domain_filter_on_zones():
    zones = {
        "example.com": [rec(1, "A", "@", "1.1.1.1")],
        "other.org": [rec(2, "A", "@", "2.2.2.2")],
    }
    p, _ = provider(zones, ["Example.com."])
    assert [e["dnsName"] for e in p.records()] == ["example.com"]
    assert p.domain_filter() == ["Example.com."]


def test_domain_filter_matches_subdomain_zone_not_suffix_lookalike():
    zones = {
        "a.example.com": [rec(1, "A", "@", "1.1.1.1")],
        "badexample.com": [rec(2, "A", "@", "2.2.2.2")],
    }
    p, _ = provider(zones, ["example.com"])
    assert [e["dnsName"] for e in p.records()] == ["a.example.com"]


# --- create --------------------------------------------------------------


def test_create_multi_target_default_ttl_and_apex():
    p, c = provider({"example.com": []})
    p.apply(
        {
            "create": [
                ep("example.com", "A", "1.1.1.1", "2.2.2.2"),
                ep("w.example.com", "TXT", "t", ttl=60),
            ]
        }
    )
    assert c.calls == [
        ("create", "example.com", {"type": "A", "name": "@", "data": "1.1.1.1", "ttl": 300}),
        ("create", "example.com", {"type": "A", "name": "@", "data": "2.2.2.2", "ttl": 300}),
        ("create", "example.com", {"type": "TXT", "name": "w", "data": "t", "ttl": 60}),
    ]


def test_create_cname_gets_trailing_dot_and_mx_priority():
    p, c = provider({"example.com": []})
    p.apply(
        {
            "create": [
                ep("b.example.com", "CNAME", "lb.example.net"),
                ep("example.com", "MX", "10 mail.example.com"),
            ]
        }
    )
    assert c.calls == [
        (
            "create",
            "example.com",
            {"type": "CNAME", "name": "b", "data": "lb.example.net.", "ttl": 300},
        ),
        (
            "create",
            "example.com",
            {"type": "MX", "name": "@", "data": "mail.example.com.", "ttl": 300, "priority": 10},
        ),
    ]


# --- update --------------------------------------------------------------


def test_update_edits_matching_creates_new_deletes_removed():
    p, c = provider(
        {"example.com": [rec(1, "A", "w", "1.1.1.1", 300), rec(2, "A", "w", "9.9.9.9", 300)]}
    )
    p.apply(
        {
            "updateOld": [ep("w.example.com", "A", "1.1.1.1", "9.9.9.9")],
            "updateNew": [ep("w.example.com", "A", "1.1.1.1", "3.3.3.3", ttl=60)],
        }
    )
    body = {"type": "A", "name": "w", "ttl": 60}
    assert c.calls == [
        ("create", "example.com", {**body, "data": "3.3.3.3"}),
        ("edit", "example.com", 1, {**body, "data": "1.1.1.1"}),
        ("delete", "example.com", 2),
    ]


def test_update_matches_case_and_dot_insensitively():
    p, c = provider({"example.com": [rec(1, "CNAME", "b", "Lb.Example.net.")]})
    p.apply({"updateNew": [ep("b.example.com", "CNAME", "lb.example.NET", ttl=120)]})
    assert c.calls == [
        (
            "edit",
            "example.com",
            1,
            {"type": "CNAME", "name": "b", "data": "lb.example.NET.", "ttl": 120},
        )
    ]


def test_update_mx_matches_existing_record():
    p, c = provider({"example.com": [rec(1, "MX", "@", "mail.example.com.", 300, 10)]})
    p.apply({"updateNew": [ep("example.com", "MX", "10 mail.example.com", ttl=60)]})
    assert [x[0] for x in c.calls] == ["edit"]


# --- delete --------------------------------------------------------------


def test_delete_only_matching_targets():
    p, c = provider({"example.com": [rec(1, "A", "w", "1.1.1.1"), rec(2, "A", "w", "2.2.2.2")]})
    p.apply({"delete": [ep("w.example.com", "A", "2.2.2.2")]})
    assert c.calls == [("delete", "example.com", 2)]


def test_delete_cname_only_deletes_matching_record():
    p, c = provider(
        {
            "example.com": [
                rec(1, "CNAME", "b", "keep.example.net."),
                rec(2, "CNAME", "b", "gone.example.net."),
            ]
        }
    )
    p.apply({"delete": [ep("b.example.com", "CNAME", "GONE.example.net")]})
    assert c.calls == [("delete", "example.com", 2)]


# --- zone selection --------------------------------------------------------


def test_longest_suffix_zone_wins():
    p, c = provider({"example.com": [], "sub.example.com": []})
    p.apply({"create": [ep("a.sub.example.com", "A", "1.1.1.1")]})
    assert c.calls == [
        ("create", "sub.example.com", {"type": "A", "name": "a", "data": "1.1.1.1", "ttl": 300})
    ]


def test_endpoint_without_zone_skipped():
    p, c = provider({"example.com": []})
    p.apply({"create": [ep("x.nowhere.org", "A", "1.1.1.1")], "delete": None})
    assert c.calls == []


def test_order_is_creates_updates_deletes():
    p, c = provider({"example.com": [rec(1, "A", "u", "1.1.1.1"), rec(2, "A", "d", "2.2.2.2")]})
    p.apply(
        {
            "delete": [ep("d.example.com", "A", "2.2.2.2")],
            "updateNew": [ep("u.example.com", "A", "1.1.1.1")],
            "create": [ep("c.example.com", "A", "3.3.3.3")],
        }
    )
    assert [x[0] for x in c.calls] == ["create", "edit", "delete"]


@pytest.mark.parametrize("changes", [{}, {"create": None}])
def test_empty_changes(changes):
    p, c = provider({"example.com": []})
    p.apply(changes)
    assert c.calls == []


def test_changes_keys_case_insensitive():
    p, c = provider({"example.com": []})
    p.apply({"Create": [ep("a.example.com", "A", "1.1.1.1")]})
    assert len(c.calls) == 1


def test_change_counter(counter_points):
    p, _ = provider(
        {
            "example.com": [
                rec(1, "A", "u", "1.1.1.1"),
                rec(2, "A", "d", "2.2.2.2"),
                rec(3, "A", "d", "9.9.9.9"),
            ]
        }
    )
    p.apply(
        {
            "create": [
                ep("a.example.com", "A", "1.1.1.1", "2.2.2.2"),
                ep("t.example.com", "TXT", "x"),
            ],
            "updateNew": [ep("u.example.com", "A", "1.1.1.1")],
            "delete": [ep("d.example.com", "A", "2.2.2.2", "9.9.9.9")],
        }
    )
    z = "example.com"
    assert counter_points() == {
        ("create", "A", z): 2,
        ("create", "TXT", z): 1,
        ("update", "A", z): 1,
        ("delete", "A", z): 2,
    }
