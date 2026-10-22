"""The client safety-number derivation, exercised through Node.

The number is computed in the browser from two DTLS fingerprints, so the only honest test runs
the shipped file rather than a Python copy of its algorithm. Node is not a project dependency:
these tests skip where it is absent, the same way the migration rehearsal skips without a
scratch database.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[1] / "app" / "static" / "js" / "safety-number.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="Node.js is required to exercise the browser safety-number module"
)

LOCAL_SDP = (
    "v=0\r\n"
    "o=- 1 2 IN IP4 127.0.0.1\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "a=fingerprint:sha-256 AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:"
    "AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99\r\n"
)
REMOTE_SDP = (
    "v=0\r\n"
    "o=- 3 4 IN IP4 127.0.0.1\r\n"
    "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
    "a=fingerprint:sha-256 11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00:"
    "11:22:33:44:55:66:77:88:99:AA:BB:CC:DD:EE:FF:00\r\n"
)
SUBSTITUTED_SDP = REMOTE_SDP.replace("11:22:33", "99:88:77")


def node_value(expression: str, **variables):
    prelude = "".join(
        f"const {name} = {json.dumps(value)};" for name, value in variables.items()
    )
    script = (
        f"const safety = require({json.dumps(str(MODULE))});{prelude}"
        f"(async () => console.log(JSON.stringify(await (async () => ({expression}))())))();"
    )
    completed = subprocess.run(
        [NODE, "-e", script], capture_output=True, text=True, timeout=30, check=False
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def test_both_sides_derive_the_same_number():
    result = node_value(
        "{ local: await safety.safetyNumber(local, remote), "
        "remote: await safety.safetyNumber(remote, local) }",
        local=LOCAL_SDP,
        remote=REMOTE_SDP,
    )
    assert result["local"] == result["remote"]
    assert len(result["local"]) == 16
    assert result["local"].isdigit()


def test_a_substituted_fingerprint_changes_the_number():
    result = node_value(
        "{ honest: await safety.safetyNumber(local, remote), "
        "substituted: await safety.safetyNumber(local, substituted) }",
        local=LOCAL_SDP,
        remote=REMOTE_SDP,
        substituted=SUBSTITUTED_SDP,
    )
    assert result["honest"] != result["substituted"]


def test_a_missing_fingerprint_yields_no_number():
    result = node_value(
        "{ nothing: await safety.safetyNumber('', ''), half: await safety.safetyNumber(local, '') }",
        local=LOCAL_SDP,
    )
    assert result == {"nothing": None, "half": None}


def test_the_number_is_grouped_for_reading():
    assert node_value("safety.format('1234567890123456')") == "1234 5678 9012 3456"
