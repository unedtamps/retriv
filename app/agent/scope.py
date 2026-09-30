"""Scope helpers for the answer path.

The RS256 delegation token is gone: the route derives the workspace scope
from the caller's own memberships and passes it straight to the harness,
so there is no signature to verify and no second service to trust.

The cap stays. A scope wide enough to be worth rejecting is also a scope
wide enough to be a denial-of-service risk against the vector search.
"""

MAX_SCOPE_WORKSPACES = 1000
