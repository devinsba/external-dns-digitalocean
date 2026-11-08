import pytest

from external_dns_digitalocean.digitalocean import DigitalOceanClient, DOError


def client(fake):
    return DigitalOceanClient("tok", fake.url)


def test_auth_header_and_user_agent(make_fake_do):
    fake = make_fake_do({"example.com": []})
    client(fake).list_zones()
    headers = {k.lower(): v for k, v in fake.requests[0][2].items()}
    assert headers["authorization"] == "Bearer tok"
    assert headers["user-agent"] == "external-dns-digitalocean"
    assert fake.requests[0][1] == "/v2/domains?per_page=200"


def test_pagination_followed(make_fake_do):
    zones = {
        f"z{i}.com": [{"id": i, "type": "A", "name": "@", "data": "1.1.1.1"}] for i in range(5)
    }
    fake = make_fake_do(zones, page_size=2)
    c = client(fake)
    assert c.list_zones() == list(zones)
    fake.zones["z0.com"] = [{"id": i, "type": "A", "name": f"n{i}", "data": "x"} for i in range(5)]
    assert len(c.list_records("z0.com")) == 5


def test_create_edit_delete_requests(make_fake_do):
    fake = make_fake_do({"example.com": []})
    c = client(fake)
    body = {"type": "MX", "name": "@", "data": "m.example.com.", "ttl": 300, "priority": 10}
    c.create_record("example.com", body)
    rid = fake.zones["example.com"][0]["id"]
    c.edit_record("example.com", rid, {**body, "ttl": 60})
    c.delete_record("example.com", rid)
    reqs = [(m, p, b) for m, p, _, b in fake.requests]
    assert reqs == [
        ("POST", "/v2/domains/example.com/records", body),
        ("PUT", f"/v2/domains/example.com/records/{rid}", {**body, "ttl": 60}),
        ("DELETE", f"/v2/domains/example.com/records/{rid}", None),
    ]
    assert fake.zones["example.com"] == []
    ctype = {k.lower(): v for k, v in fake.requests[0][2].items()}["content-type"]
    assert ctype == "application/json"


def test_non_2xx_raises_with_status_and_body(make_fake_do):
    fake = make_fake_do()
    fake.fail_with = 429
    with pytest.raises(DOError, match="429.*kaboom"):
        client(fake).list_zones()
