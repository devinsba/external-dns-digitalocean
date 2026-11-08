import json
import urllib.error
import urllib.request
from typing import Any


class DOError(Exception):
    pass


class DigitalOceanClient:
    def __init__(self, token: str, api_url: str, timeout: float = 30):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, url: str, body: dict | None = None) -> Any:
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "external-dns-digitalocean",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raise DOError(f"{method} {url} -> {e.code}: {e.read().decode(errors='replace')}") from e
        except urllib.error.URLError as e:
            raise DOError(f"{method} {url} failed: {e.reason}") from e
        return json.loads(raw) if raw else None

    def _list(self, path: str, key: str) -> list[dict]:
        items: list[dict] = []
        url: str | None = f"{self.api_url}{path}?per_page=200"
        while url:
            page = self._request("GET", url)
            items += page.get(key) or []
            url = ((page.get("links") or {}).get("pages") or {}).get("next")
        return items

    def list_zones(self) -> list[str]:
        return [d["name"] for d in self._list("/domains", "domains")]

    def list_records(self, zone: str) -> list[dict]:
        return self._list(f"/domains/{zone}/records", "domain_records")

    def create_record(self, zone: str, body: dict) -> None:
        self._request("POST", f"{self.api_url}/domains/{zone}/records", body)

    def edit_record(self, zone: str, record_id: int, body: dict) -> None:
        self._request("PUT", f"{self.api_url}/domains/{zone}/records/{record_id}", body)

    def delete_record(self, zone: str, record_id: int) -> None:
        self._request("DELETE", f"{self.api_url}/domains/{zone}/records/{record_id}")
