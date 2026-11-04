import pytest
from worker.run_plan import PlanError, validate_plan

OK = [{"action": "login", "role": "support"}, {"action": "goto", "path": "/orders/1001"},
      {"action": "fill", "testid": "refund-amount", "value": "15000"},
      {"action": "click", "testid": "refund-submit"}, {"action": "expect", "testid": "error"}]


def test_valid_plan_accepted():
    assert validate_plan(OK) == OK


@pytest.mark.parametrize("bad", [
    [{"action": "shell", "cmd": "rm -rf /"}],
    [{"action": "eval", "js": "fetch('http://evil')"}],
    [{"action": "goto", "path": "http://evil.example/"}],
    [{"action": "goto", "path": "//evil.example/"}],
    [{"action": "goto", "path": "orders/1"}],
    [{"action": "click", "testid": "a b"}],
    [{"action": "click", "testid": "x", "extra": "y"}],
    [{"action": "login", "role": "root"}],
    [{"action": "fill", "testid": "x"}],
    [],
    "not a list",
    [{"action": "click", "testid": "x"}] * 31,
])
def test_bad_plans_rejected(bad):
    with pytest.raises(PlanError):
        validate_plan(bad)
