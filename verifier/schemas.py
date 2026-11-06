"""Pydantic schemas. invariant_type comes from a FIXED list; anything else is rejected."""
from typing import Literal
from pydantic import BaseModel, ConfigDict

# Extend this list as policies are added (P-02, P-03, P-04, P-05).
InvariantType = Literal["approval_required_over_threshold", "owner_only_access",
                       "role_change_admin_only"]


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str
    source_clause: str
    actor: str
    action: str
    params: dict
    invariant_type: InvariantType


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    test_id: str
    rule_id: str
    actor_role: str
    preconditions: dict
    steps: list[dict] = []
    expected_outcome: Literal["blocked", "allowed"]
    invariant_type: InvariantType
