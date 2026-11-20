"""
Tests for the CBE permission predicates.

Fixtures live in this directory's conftest.py.
"""
import importlib

import pytest
import rules
from django.contrib.auth.models import AnonymousUser
from opaque_keys.edx.keys import CourseKey

import openedx_learning.rules
from openedx_learning.applets.cbe import rules as cbe_rules
from openedx_learning.applets.cbe.rules import can_view_competency_rule_profile
from openedx_learning.models import CompetencyRuleProfile, CompetencyTaxonomy, RuleType
from openedx_tagging.rules import UserType

pytestmark = pytest.mark.django_db

VIEW_RULE_PROFILE = "openedx_learning.view_competencyruleprofile"
# The name openedx-platform binds its own rule to, so it is spelled out here rather than imported.
VIEW_COURSE_COMPETENCY_CRITERIA = "openedx_learning.view_course_competency_criteria"
COURSE_KEY = CourseKey.from_string("course-v1:Org1+Python100+Fall2026")
OTHER_COURSE_KEY = CourseKey.from_string("course-v1:Org1+Python200+Spring2027")

# A valid payload for the rows these tests create themselves.
GRADE_PAYLOAD = {"op": "gte", "value": 0.6, "scale": "percent"}


@pytest.mark.parametrize("user_fixture", ["staff_user", "user"])
def test_staff_and_non_staff_may_view_the_collection_and_the_system_default(
    request: pytest.FixtureRequest,
    default_rule_profile: CompetencyRuleProfile,
    user_fixture: str,
) -> None:
    caller = request.getfixturevalue(user_fixture)

    assert can_view_competency_rule_profile(caller) is True
    assert can_view_competency_rule_profile(caller, default_rule_profile) is True


def test_a_profile_on_a_disabled_taxonomy_stays_administrator_only(
    staff_user: UserType,
    user: UserType,
) -> None:
    disabled_taxonomy = CompetencyTaxonomy.objects.create(name="Retired", export_id="retired-v1", enabled=False)
    profile = CompetencyRuleProfile.objects.create(
        competency_taxonomy=disabled_taxonomy,
        rule_type=RuleType.GRADE,
        rule_payload=dict(GRADE_PAYLOAD),
    )

    assert can_view_competency_rule_profile(user, profile) is False
    assert can_view_competency_rule_profile(staff_user, profile) is True


@pytest.mark.parametrize("scope_field, scope_fixture", [("course", "course_run"), ("organization", "organization")])
def test_course_and_organization_scoped_profiles_are_refused_to_everyone(
    request: pytest.FixtureRequest,
    staff_user: UserType,
    user: UserType,
    scope_field: str,
    scope_fixture: str,
) -> None:
    profile = CompetencyRuleProfile.objects.create(
        rule_type=RuleType.GRADE,
        rule_payload=dict(GRADE_PAYLOAD),
        **{scope_field: request.getfixturevalue(scope_fixture)},
    )

    assert can_view_competency_rule_profile(user, profile) is False
    assert can_view_competency_rule_profile(staff_user, profile) is False


def test_anonymous_callers_are_refused_by_the_registered_permission(
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    anonymous = AnonymousUser()

    assert anonymous.has_perm(VIEW_RULE_PROFILE) is False
    assert anonymous.has_perm(VIEW_RULE_PROFILE, default_rule_profile) is False


# ---------------------------------------------------------------------------------------------
# view_course_competency_criteria


def test_the_course_criteria_permission_name_is_exported_under_its_contract_name() -> None:
    """N2: openedx-platform binds this exact name, so it is part of the public contract."""
    assert cbe_rules.VIEW_COURSE_COMPETENCY_CRITERIA == VIEW_COURSE_COMPETENCY_CRITERIA
    assert "VIEW_COURSE_COMPETENCY_CRITERIA" in cbe_rules.__all__
    assert openedx_learning.rules.VIEW_COURSE_COMPETENCY_CRITERIA == VIEW_COURSE_COMPETENCY_CRITERIA


def test_the_course_criteria_permission_is_registered_by_default() -> None:
    """N2"""
    assert rules.perm_exists(VIEW_COURSE_COMPETENCY_CRITERIA)


def test_anonymous_callers_are_refused_the_course_criteria_by_default() -> None:
    """N2: the default rule is rules.is_authenticated."""
    assert rules.perm_exists(VIEW_COURSE_COMPETENCY_CRITERIA)
    assert AnonymousUser().has_perm(VIEW_COURSE_COMPETENCY_CRITERIA, COURSE_KEY) is False


@pytest.mark.parametrize("user_fixture", ["staff_user", "user"])
def test_any_signed_in_user_may_read_the_course_criteria_by_default(
    request: pytest.FixtureRequest,
    user_fixture: str,
) -> None:
    """N2: neither enrollment nor staff membership matters under the default rule."""
    caller = request.getfixturevalue(user_fixture)

    assert caller.has_perm(VIEW_COURSE_COMPETENCY_CRITERIA, COURSE_KEY) is True


@pytest.mark.usefixtures("restore_permission_registry")
def test_a_host_rule_bound_after_core_loaded_replaces_the_default(user: UserType) -> None:
    """E7: the host's set_perm wins, and its rule is asked with the course key."""
    assert rules.perm_exists(VIEW_COURSE_COMPETENCY_CRITERIA)
    rules.set_perm(VIEW_COURSE_COMPETENCY_CRITERIA, lambda _user, course_key: course_key == COURSE_KEY)

    assert user.has_perm(VIEW_COURSE_COMPETENCY_CRITERIA, COURSE_KEY) is True
    assert user.has_perm(VIEW_COURSE_COMPETENCY_CRITERIA, OTHER_COURSE_KEY) is False


@pytest.mark.usefixtures("restore_permission_registry")
def test_a_host_rule_registered_before_core_loads_stays_in_force(user: UserType) -> None:
    """
    E6: the host registers the name first, then core's rules module loads.

    The module is reloaded to play that second step. It registers the other permission too, which the
    reload would find already registered, so that one is removed first; it is not under test.
    """
    rules.remove_perm(VIEW_COURSE_COMPETENCY_CRITERIA)
    rules.add_perm(VIEW_COURSE_COMPETENCY_CRITERIA, lambda _user, course_key: course_key == COURSE_KEY)
    rules.remove_perm(VIEW_RULE_PROFILE)

    importlib.reload(cbe_rules)

    assert user.has_perm(VIEW_COURSE_COMPETENCY_CRITERIA, COURSE_KEY) is True
    assert user.has_perm(VIEW_COURSE_COMPETENCY_CRITERIA, OTHER_COURSE_KEY) is False
