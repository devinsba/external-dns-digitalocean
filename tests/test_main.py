import subprocess
import sys


def test_missing_token_exits_nonzero():
    r = subprocess.run(
        [sys.executable, "-m", "external_dns_digitalocean"],
        env={"PATH": ""},
        capture_output=True,
        text=True,
    )
    assert r.returncode != 0
    assert "DO_TOKEN" in r.stderr
