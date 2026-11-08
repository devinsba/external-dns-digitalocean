from typing import Any

import requests


class DOError(Exception):
    pass


class DigitalOceanClient:
    def __init__(self, token: str, api_url: str, timeout: float = 30):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, url: str, body: dict | None = None) -> Any:
        try:
            resp = requests.request(
                method,
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "User-Agent": "external-dns-digitalocean",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise DOError(f"{method} {url} failed: {e}") from e
        if not resp.ok:
            raise DOError(f"{method} {url} -> {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else None

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
