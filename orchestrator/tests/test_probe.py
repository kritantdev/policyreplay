import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from orchestrator.sandbox_run import probe_ok  # noqa: E402

GOOD = ("{'rootfs_write': 'blocked: Read-only file system', 'internet': 'blocked: [Errno 101] Network is "
        "unreachable', 'metadata': 'blocked: [Errno 101] Network is unreachable', 'env_var_names': ['HOME'], "
        "'secret_like_env_vars': [], 'vultr_vars': [], 'chromium': 'ok'}")


def test_good_probe_passes():
    assert probe_ok(0, GOOD)


def test_each_failed_claim_fails_the_probe():
    for bad in [GOOD.replace("blocked: Read-only", "ALLOWED (bad) Read-only"),
                GOOD.replace("'internet': 'blocked", "'internet': 'ALLOWED (bad)"),
                GOOD.replace("'metadata': 'blocked", "'metadata': 'ALLOWED (bad)"),
                GOOD.replace("'secret_like_env_vars': []", "'secret_like_env_vars': ['API_KEY']"),
                GOOD.replace("'vultr_vars': []", "'vultr_vars': ['VULTR_INFERENCE_KEY']"),
                GOOD.replace("'chromium': 'ok'", "'chromium': 'crashed'")]:
        assert not probe_ok(0, bad)
    assert not probe_ok(1, GOOD) and not probe_ok(0, "garbage") and not probe_ok(0, "")
