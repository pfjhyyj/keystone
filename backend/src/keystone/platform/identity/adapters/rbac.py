"""Ephemeral Casbin engine construction; granted permissions are supplied by Service."""

import casbin
from casbin import Enforcer as Enforcer
from casbin import Model

MODEL = """
[request_definition]
r = sub, permission
[policy_definition]
p = role, permission
[role_definition]
g = _, _
[policy_effect]
e = some(where (p.eft == allow))
[matchers]
m = g(r.sub, p.role) && r.permission == p.permission
"""


def enforcer_for_roles(user_id, roles):
    model = Model()
    model.load_model_from_text(MODEL)
    enforcer = casbin.Enforcer(model)
    enforcer.enable_auto_save(False)
    grants = set()
    for role_id, permissions in roles.items():
        enforcer.add_grouping_policy(str(user_id), str(role_id))
        for permission in permissions:
            enforcer.add_policy(str(role_id), permission)
            grants.add(permission)
    return enforcer, grants
