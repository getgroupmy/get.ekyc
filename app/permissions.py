"""Who may do what in the console.

Each console user has one role. A role is a set of permissions plus a scope:
"all" sees every API client and its verifications, "own" only the clients the
user created (and their verifications). Signing in with the admin token is the
owner role. Checks run on the server; the console only hides what you cannot use.
"""
from __future__ import annotations

from dataclasses import dataclass

PERMISSIONS = {
    "verifications.view": "See verifications and their results",
    "media.view": "See ID and selfie images",
    "verifications.decide": "Approve or reject, mark as test",
    "verifications.erase": "Erase personal data of any verification",
    "fraud.block": "Block a person's face for every app",
    "clients.view": "See API clients",
    "clients.manage": "Create, revoke and delete API clients",
    "tests.run": "Run Try it and delete test runs",
    "samples.manage": "Manage saved test documents",
    "learning.view": "See what the engine learned from reviews",
    "compliance.view": "See sanctions list status",
    "compliance.manage": "Refresh the sanctions list",
    "audit.view": "Check the audit trail",
    "admins.manage": "Create console users and set their roles",
    "system.update": "Update get.ekyc to the latest version",
    "improvement.export": "Download the last days' real checks to improve the engine",
}

_ALL = set(PERMISSIONS)
ROLES: dict[str, dict] = {
    "owner": {"perms": _ALL, "scope": "all", "label": "Owner: everything, including console users"},
    "admin": {"perms": _ALL - {"admins.manage", "system.update", "improvement.export"}, "scope": "all", "label": "Admin: everything except console users"},
    "reviewer": {"perms": {"verifications.view", "media.view", "verifications.decide", "fraud.block",
                           "learning.view", "compliance.view"},
                 "scope": "all", "label": "Reviewer: review and decide cases; no API keys"},
    "developer": {"perms": {"clients.view", "clients.manage", "tests.run", "samples.manage", "verifications.view",
                            "media.view"},
                  "scope": "own", "label": "Developer: own API keys and their cases, Try it"},
    "auditor": {"perms": {"verifications.view", "media.view", "clients.view", "audit.view", "learning.view",
                          "compliance.view"},
                "scope": "all", "label": "Auditor: read only"},
}


@dataclass(frozen=True)
class Ctx:
    actor: str  # what the audit trail records: "admin" (token) or "admin:<username>"
    username: str | None
    role: str

    @property
    def perms(self) -> set[str]:
        return ROLES[self.role]["perms"]

    @property
    def sees_all(self) -> bool:
        return ROLES[self.role]["scope"] == "all"

    def can(self, perm: str) -> bool:
        return perm in self.perms

    @property
    def owner_tag(self) -> str:
        """What a client this user creates is recorded under."""
        return self.username or "token"
