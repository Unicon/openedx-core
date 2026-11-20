"""
Django rules-based permissions for Competency-Based Education (CBE).

Registered with the ``rules`` permission registry on import. ``src/openedx_learning/rules.py``
is what makes that import happen; see the note there.
"""
from __future__ import annotations

import rules

from openedx_tagging.rules import UserType

from .models import CompetencyRuleProfile

__all__ = [
    "VIEW_COURSE_COMPETENCY_CRITERIA",
    "can_view_competency_rule_profile",
]

# Checked with the course key, as ``user.has_perm(VIEW_COURSE_COMPETENCY_CRITERIA, course_key)``.
# openedx-platform is expected to bind its own rule to this name, so the name is part of this library's contract.
VIEW_COURSE_COMPETENCY_CRITERIA = "openedx_learning.view_course_competency_criteria"


@rules.predicate
def can_view_competency_rule_profile(
    user: UserType,
    profile: CompetencyRuleProfile | None = None,
) -> bool:
    """
    Decide who may read a rule profile, by the profile's scope.

    Course authors, not only platform staff, need to read rule profiles, so the collection and
    the system default are open to anyone signed in. A taxonomy-scoped profile is as visible as
    its taxonomy, which is the gate the criteria tree endpoint uses too, through
    ``oel_tagging.view_taxonomy`` on the tag's taxonomy; a profile on a disabled taxonomy stays
    admin-only. Course- and organization-scoped profiles are refused to everyone until #645
    decides who may see them.

    The signed-in requirement is added where the permission is registered, below, so ``has_perm``
    refuses an anonymous caller whatever view sits in front of it. This predicate alone does not
    check it.

    The taxonomy case asks ``has_perm`` rather than calling ``can_view_taxonomy``: openedx-platform
    replaces that rule with an org-aware one via ``rules.set_perm``, which a direct call skips.
    """
    if profile is None:
        # Listing. Any row-level filtering happens in the queryset.
        return True
    if profile.competency_taxonomy_id is not None:
        return user.has_perm("oel_tagging.view_taxonomy", profile.competency_taxonomy)
    if profile.course_id is None and profile.organization_id is None:
        # The system default: anyone who is signed in may read it.
        return True
    # Course- and organization-scoped profiles: no authoring flow creates these yet, and neither
    # tagging permission describes who may see them. Refused until #645 decides.
    return False


rules.add_perm(
    "openedx_learning.view_competencyruleprofile",
    rules.is_authenticated & can_view_competency_rule_profile,
)

# Registered only when the name is free, so the host's rule wins whichever module loads first. Loaded
# first, this default is replaced by the host's later ``rules.set_perm``. Loaded second, a plain
# ``add_perm`` would raise ``KeyError`` and ``set_perm`` would undo the host's rule.
if not rules.perm_exists(VIEW_COURSE_COMPETENCY_CRITERIA):
    rules.add_perm(VIEW_COURSE_COMPETENCY_CRITERIA, rules.is_authenticated)
