"""
HTTP tests for the learner endpoint that lists a course run's competency criteria.

The API behind it has its own tests in test_course_criteria.py, so most of these pin what only the HTTP
layer decides: authentication, key parsing, the permission, status codes, and the wire format.

Fixtures live in this directory's conftest.py, the tree builders in utils.py.
"""
import pytest
import rules
from django.contrib.auth import get_user_model
from django.urls import Resolver404, resolve, reverse
from opaque_keys.edx.keys import CourseKey
from rest_framework import status
from rest_framework.test import APIClient

from openedx_catalog.models import CourseRun
from openedx_learning.models import CompetencyCriteriaGroup, CompetencyRuleProfile, CompetencyTaxonomy
from openedx_tagging.models import Tag

from .utils import course_key_of, make_branch, make_competency_taxonomy, make_leaf_criterion, make_tag, usage_key

pytestmark = pytest.mark.django_db

# The name openedx-platform binds its own rule to, so it is spelled out here rather than imported.
VIEW_COURSE_COMPETENCY_CRITERIA = "openedx_learning.view_course_competency_criteria"

EMPTY_BODY: dict[str, list[object]] = {"taxonomies": [], "tags": [], "groups": [], "criteria": []}
# An urlconf that mounts only the learner routes, like an LMS.
LMS_URLCONF = "tests.openedx_learning.applets.cbe.lms_urls"
MALFORMED_KEY_BODY = {"course_key": ["The course run key is malformed."]}


@pytest.fixture(name="user_client")
def _user_client(user) -> APIClient:
    """A REST client acting as `user`, a signed-in caller who is neither enrolled nor on any staff."""
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def criteria_url(course_key: CourseKey | str) -> str:
    """Return the endpoint's path for `course_key`, resolved through the URL name."""
    return reverse("cbe_learner:course-competency-criteria", kwargs={"course_key": str(course_key)})


def literal_path(raw_course_key: str) -> str:
    """Return the endpoint's path with `raw_course_key` pasted in exactly as written, unquoted."""
    return f"/api/cbe/v1/courses/{raw_course_key}/competency-criteria/"


def make_course_with_criteria(
    course_run: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> str:
    """Give `course_run` one criterion on `tag` and return the criterion's usage key."""
    criterion = make_leaf_criterion(tag, course_run, default_rule_profile, "quiz1")
    return criterion.object_tag.object_id


# =============================================================================================
# Access under openedx-core's default permission
# =============================================================================================


def test_a_caller_who_is_not_signed_in_is_rejected_with_no_competency_data(
    api_client: APIClient, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC1"""
    usage = make_course_with_criteria(course_x, tag, default_rule_profile)

    response = api_client.get(criteria_url(course_key_of(course_x)))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED
    assert set(response.json()) == {"detail"}
    assert usage not in response.content.decode()


def test_any_signed_in_user_can_read_the_criteria_under_the_default_permission(
    user_client: APIClient, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC2: a user who is not staff and has no enrollment in the course run still gets the full result."""
    usage = make_course_with_criteria(course_x, tag, default_rule_profile)

    response = user_client.get(criteria_url(course_key_of(course_x)))

    assert response.status_code == status.HTTP_200_OK
    assert [criterion["object_id"] for criterion in response.json()["criteria"]] == [usage]
    assert [competency["value"] for competency in response.json()["tags"]] == ["Writing Poetry"]


@pytest.mark.usefixtures("restore_permission_registry")
def test_a_host_rule_that_refuses_gives_403_and_no_competency_data(
    user_client: APIClient, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC6a, E7: a host that rebinds the permission after core loaded decides, and a refusal returns nothing."""
    usage = make_course_with_criteria(course_x, tag, default_rule_profile)
    rules.set_perm(VIEW_COURSE_COMPETENCY_CRITERIA, rules.always_deny)

    response = user_client.get(criteria_url(course_key_of(course_x)))

    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert set(response.json()) == {"detail"}
    assert usage not in response.content.decode()


@pytest.mark.usefixtures("restore_permission_registry")
def test_a_host_rule_that_decides_per_user_lets_one_caller_in_and_refuses_another(
    user_client: APIClient, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC6a: the host's rule sees the signed-in user, so an enrolled learner is read while a stranger is refused."""
    make_course_with_criteria(course_x, tag, default_rule_profile)
    enrolled_learner = get_user_model().objects.create(username="enrolled_learner")
    enrolled_client = APIClient()
    enrolled_client.force_authenticate(user=enrolled_learner)
    rules.set_perm(VIEW_COURSE_COMPETENCY_CRITERIA, lambda user, _course_key: user == enrolled_learner)

    assert enrolled_client.get(criteria_url(course_key_of(course_x))).status_code == status.HTTP_200_OK
    assert user_client.get(criteria_url(course_key_of(course_x))).status_code == status.HTTP_403_FORBIDDEN


@pytest.mark.usefixtures("restore_permission_registry")
def test_the_permission_is_checked_against_the_parsed_course_key(user_client: APIClient, user) -> None:
    """N2: the host's rule receives the signed-in user and a CourseKey, not the string from the path."""
    received = []

    def recording_rule(rule_user, course_key) -> bool:
        received.append((rule_user, course_key))
        return True

    rules.set_perm(VIEW_COURSE_COMPETENCY_CRITERIA, recording_rule)
    course_key = CourseKey.from_string("course-v1:Org1+Python100+Fall2026")

    user_client.get(criteria_url(course_key))

    assert received == [(user, course_key)]
    assert isinstance(received[0][1], CourseKey)


@pytest.mark.usefixtures("restore_permission_registry")
def test_a_refused_request_reads_no_competency_data(
    user_client: APIClient, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
    django_assert_num_queries,
) -> None:
    """N12: the permission is checked before any query, so a refused caller costs the database nothing."""
    make_course_with_criteria(course_x, tag, default_rule_profile)
    rules.set_perm(VIEW_COURSE_COMPETENCY_CRITERIA, rules.always_deny)

    with django_assert_num_queries(0):
        response = user_client.get(criteria_url(course_key_of(course_x)))

    assert response.status_code == status.HTTP_403_FORBIDDEN


# =============================================================================================
# Who is asking does not change the result
# =============================================================================================


def test_every_permitted_caller_gets_the_same_result(
    user_client: APIClient, staff_client: APIClient, course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC22, core side: two different signed-in users and a staff user receive identical bodies."""
    make_branch(make_tag(competency_taxonomy, "Communication"), course_x, default_rule_profile, "comm_quiz")
    make_branch(make_tag(competency_taxonomy, "Teamwork"), course_x, default_rule_profile, "team_quiz")
    other_learner_client = APIClient()
    other_learner_client.force_authenticate(user=get_user_model().objects.create(username="other_learner"))

    url = criteria_url(course_key_of(course_x))
    bodies = [client.get(url).json() for client in (user_client, other_learner_client, staff_client)]

    assert bodies[0]["criteria"]
    assert bodies[0] == bodies[1] == bodies[2]


# =============================================================================================
# Course resolution
# =============================================================================================


def test_a_malformed_course_run_key_is_rejected_with_a_message_and_no_data(
    user_client: APIClient, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC8"""
    make_course_with_criteria(course_x, tag, default_rule_profile)

    response = user_client.get(literal_path("not-a-course-key"))

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == MALFORMED_KEY_BODY


def test_a_course_run_in_the_catalog_without_criteria_returns_an_empty_body(
    user_client: APIClient, course_x: CourseRun,
) -> None:
    """AC9a"""
    response = user_client.get(criteria_url(course_key_of(course_x)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == EMPTY_BODY


def test_a_well_formed_key_matching_no_course_run_returns_an_empty_body(user_client: APIClient) -> None:
    """AC9b"""
    response = user_client.get(criteria_url("course-v1:Nowhere+Missing+Run"))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == EMPTY_BODY


# =============================================================================================
# Spellings of a valid key
# =============================================================================================


def test_a_course_key_with_a_raw_plus_resolves(
    user_client: APIClient, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E1: a plus stays a plus in the path, where a query string would have turned it into a space."""
    usage = make_course_with_criteria(course_x, tag, default_rule_profile)
    assert "+" in str(course_key_of(course_x))

    response = user_client.get(literal_path(str(course_key_of(course_x))))

    assert response.status_code == status.HTTP_200_OK
    assert [criterion["object_id"] for criterion in response.json()["criteria"]] == [usage]


def test_a_course_key_with_an_encoded_plus_resolves_to_the_same_course_run(
    user_client: APIClient, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E1: a client that percent-encodes the plus as %2B reaches the same course run."""
    usage = make_course_with_criteria(course_x, tag, default_rule_profile)

    response = user_client.get(literal_path(str(course_key_of(course_x)).replace("+", "%2B")))

    assert response.status_code == status.HTTP_200_OK
    assert [criterion["object_id"] for criterion in response.json()["criteria"]] == [usage]


def test_a_deprecated_slash_key_is_accepted_and_has_no_criteria(user_client: APIClient) -> None:
    """E2: Org/Course/Run keys contain slashes, and they reach the view instead of failing to route."""
    response = user_client.get(literal_path("Org1/Python100/Fall2026"))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == EMPTY_BODY


def test_a_ccx_key_is_accepted_and_has_no_criteria(user_client: APIClient) -> None:
    """E3: a CCX key is a course key, so it is not malformed; it just has no criteria of its own."""
    response = user_client.get(literal_path("ccx-v1:Org1+Python100+Fall2026+ccx@1"))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == EMPTY_BODY


# =============================================================================================
# Values that are not a course run key
# =============================================================================================


def assert_rejected_as_malformed(response, submitted_value: str) -> None:
    """Assert a 400 with the fixed message, which must not repeat what the caller sent."""
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json() == MALFORMED_KEY_BODY
    assert submitted_value not in response.content.decode()


def test_a_key_with_a_branch_is_rejected(user_client: APIClient) -> None:
    """E5"""
    submitted = "course-v1:Org1+Python100+Fall2026+branch@draft-branch"

    assert_rejected_as_malformed(user_client.get(literal_path(submitted)), submitted)


def test_a_key_with_a_version_is_rejected(user_client: APIClient) -> None:
    """E5"""
    submitted = "course-v1:Org1+Python100+Fall2026+version@5f3e1d2c8a1b2c3d4e5f6a7b"

    assert_rejected_as_malformed(user_client.get(literal_path(submitted)), submitted)


def test_a_version_on_its_own_is_rejected(user_client: APIClient) -> None:
    """E5"""
    submitted = "version@5f3e1d2c8a1b2c3d4e5f6a7b"

    assert_rejected_as_malformed(user_client.get(literal_path(submitted)), submitted)


def test_a_library_key_is_rejected(user_client: APIClient) -> None:
    """E5: a library-v1 key parses, but it is not a course run."""
    submitted = "library-v1:Org1+Library1"

    assert_rejected_as_malformed(user_client.get(literal_path(submitted)), submitted)


def test_a_v2_library_key_is_rejected(user_client: APIClient) -> None:
    """E5: a lib: key does not parse as a course key at all."""
    submitted = "lib:Org1:Library1"

    assert_rejected_as_malformed(user_client.get(literal_path(submitted)), submitted)


def test_a_key_over_255_characters_is_rejected(user_client: APIClient) -> None:
    """E5: the key is otherwise well-formed, so only its length gives it away."""
    submitted = "course-v1:Org1+Python100+" + "R" * 250

    assert_rejected_as_malformed(user_client.get(literal_path(submitted)), submitted)


def test_a_single_character_is_rejected(user_client: APIClient) -> None:
    """E5: junk as short as it gets still reaches the view and is answered with the fixed message."""
    assert_rejected_as_malformed(user_client.get(literal_path("x")), "x")


def test_a_malformed_value_containing_slashes_reaches_the_view_and_is_rejected(user_client: APIClient) -> None:
    """E5: the router does not turn it into a 404, because the key part of the path accepts slashes."""
    submitted = "course-v1:Org1+Python100+Fall2026/extra"

    assert_rejected_as_malformed(user_client.get(literal_path(submitted)), submitted)


# =============================================================================================
# Request order
# =============================================================================================


def test_a_caller_who_is_not_signed_in_is_rejected_before_the_key_is_parsed(api_client: APIClient) -> None:
    """N12: authentication comes first, so an anonymous caller learns nothing about what makes a key malformed."""
    response = api_client.get(literal_path("not-a-course-key"))

    assert response.status_code == status.HTTP_401_UNAUTHORIZED


@pytest.mark.usefixtures("restore_permission_registry")
def test_a_malformed_key_is_rejected_before_the_permission_is_asked(user_client: APIClient) -> None:
    """N12: the permission needs a parsed key, so a value that does not parse never reaches the host's rule."""
    asked = []

    def recording_rule(_user, course_key) -> bool:
        asked.append(course_key)
        return False

    rules.set_perm(VIEW_COURSE_COMPETENCY_CRITERIA, recording_rule)

    response = user_client.get(literal_path("not-a-course-key"))

    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert not asked


def test_a_malformed_key_costs_no_queries(user_client: APIClient, django_assert_num_queries) -> None:
    """N12: nothing is read from the database for a value that is not a course key."""
    with django_assert_num_queries(0):
        response = user_client.get(literal_path("not-a-course-key"))

    assert response.status_code == status.HTTP_400_BAD_REQUEST


# =============================================================================================
# The wire format
# =============================================================================================


def test_the_response_body_has_exactly_the_documented_shape(
    user_client: APIClient, course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    AC10, N5: every field the Progress tab needs, each reference named after the array it points into, and
    nothing else: no course key, rule profile id, override columns, object tag id, or course run id.
    """
    problem_solving = make_tag(competency_taxonomy, "Problem Solving")
    root_cause_analysis = Tag.objects.create(
        taxonomy=competency_taxonomy, value="Root Cause Analysis", external_id="PS-1.2", parent=problem_solving,
    )
    branch = make_branch(root_cause_analysis, course_x, default_rule_profile, "quiz1")
    CompetencyCriteriaGroup.objects.filter(id=branch.bottom_tier.id).update(
        name="Quizzes", ordering=2, logic_operator="AND",
    )
    (criterion,) = branch.criteria

    response = user_client.get(criteria_url(course_key_of(course_x)))

    assert response.status_code == status.HTTP_200_OK
    assert response.json() == {
        "taxonomies": [{"id": competency_taxonomy.id, "name": "Nursing"}],
        "tags": [
            {
                "id": root_cause_analysis.id,
                "taxonomy_id": competency_taxonomy.id,
                "value": "Root Cause Analysis",
                "external_id": "PS-1.2",
                "lineage": [
                    {"id": problem_solving.id, "value": "Problem Solving"},
                    {"id": root_cause_analysis.id, "value": "Root Cause Analysis"},
                ],
            },
        ],
        "groups": [
            {
                "id": branch.root.id, "parent_id": None, "tag_id": root_cause_analysis.id,
                "name": branch.root.name, "ordering": 0, "logic_operator": None,
            },
            {
                "id": branch.course_level.id, "parent_id": branch.root.id, "tag_id": root_cause_analysis.id,
                "name": branch.course_level.name, "ordering": 0, "logic_operator": None,
            },
            {
                "id": branch.bottom_tier.id, "parent_id": branch.course_level.id, "tag_id": root_cause_analysis.id,
                "name": "Quizzes", "ordering": 2, "logic_operator": "AND",
            },
        ],
        "criteria": [
            {
                "id": criterion.id,
                "group_id": branch.bottom_tier.id,
                "object_id": usage_key(course_x, "quiz1"),
                "rule_type": "Grade",
                "rule_payload": default_rule_profile.rule_payload,
            },
        ],
    }


def test_repeated_requests_list_everything_in_the_same_order(
    user_client: APIClient, course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC16: several competencies across two taxonomies come back in one order, request after request."""
    durable_skills = make_competency_taxonomy("Durable Skills")
    make_branch(make_tag(durable_skills, "Communication"), course_x, default_rule_profile, "comm_quiz", "comm_essay")
    make_branch(make_tag(competency_taxonomy, "Root Cause Analysis"), course_x, default_rule_profile, "rca_quiz")
    make_branch(make_tag(competency_taxonomy, "Triage"), course_x, default_rule_profile, "triage_quiz")
    url = criteria_url(course_key_of(course_x))

    first = user_client.get(url).json()
    second = user_client.get(url).json()

    assert len(first["taxonomies"]) == 2
    assert len(first["tags"]) == 3
    assert first == second


# =============================================================================================
# Methods
# =============================================================================================


@pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
def test_methods_that_would_write_are_not_allowed(user_client: APIClient, course_x: CourseRun, method: str) -> None:
    """E18"""
    response = getattr(user_client, method)(criteria_url(course_key_of(course_x)))

    assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


def test_head_is_allowed(user_client: APIClient, course_x: CourseRun) -> None:
    """E18"""
    response = user_client.head(criteria_url(course_key_of(course_x)))

    assert response.status_code == status.HTTP_200_OK


# =============================================================================================
# URL modules
# =============================================================================================


def test_the_route_lives_under_the_cbe_v1_prefix_with_the_key_in_the_path() -> None:
    """The planned sibling statuses endpoint uses the same courses/<course_key>/ shape, so the key stays literal."""
    assert criteria_url("course-v1:Org1+Python100+Fall2026") == (
        "/api/cbe/v1/courses/course-v1:Org1+Python100+Fall2026/competency-criteria/"
    )


def test_the_learner_urls_resolve_the_learner_route() -> None:
    """AC7: what an LMS mounts does include the endpoint."""
    match = resolve(literal_path("course-v1:Org1+Python100+Fall2026"), urlconf=LMS_URLCONF)

    assert match.view_name == "cbe_learner:course-competency-criteria"
    assert match.kwargs == {"course_key": "course-v1:Org1+Python100+Fall2026"}


@pytest.mark.parametrize(
    "authoring_path",
    [
        "/api/cbe/v1/rule_profiles/",
        "/api/cbe/v1/competencies/1/criteria/",
        "/api/cbe/v1/competencies/1/criteria/1/",
        "/api/cbe/v1/competencies/1/criteria-groups/",
        "/api/cbe/v1/competencies/1/criteria-groups/1/",
        "/api/cbe/v1/competencies/1/blocked-courses/",
        "/api/cbe/v1/criteria-groups/1/criteria/bulk-update/",
    ],
)
def test_the_learner_urls_serve_no_authoring_route(authoring_path: str) -> None:
    """AC7: an LMS that mounts only the learner URLs answers every authoring route with "no such route"."""
    resolve(authoring_path)  # The same path does exist where the authoring URLs are mounted.

    with pytest.raises(Resolver404):
        resolve(authoring_path, urlconf=LMS_URLCONF)


def test_the_authoring_routes_still_live_at_cbe_v1() -> None:
    """N10: adding learner routes did not move the authoring ones that the CMS mounts."""
    assert reverse("cbe:rule_profile-list") == "/api/cbe/v1/rule_profiles/"
