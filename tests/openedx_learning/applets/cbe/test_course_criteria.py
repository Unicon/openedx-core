"""
Tests for get_course_competency_criteria(), the read behind the learner endpoint that lists a course
run's competency criteria.

Vocabulary, as in the ticket: a competency is a Tag in a competency taxonomy. Each competency has one root
group, shared by every course run. Beneath it sits one course-level group per course run, and beneath that
the bottom-tier groups, which hold the criteria.

Fixtures live in this directory's conftest.py, the tree builders in utils.py.
"""
import dataclasses
import inspect
from collections.abc import Iterator
from datetime import datetime
from typing import NamedTuple

import pytest
import rules
from opaque_keys.edx.keys import CourseKey
from rules.permissions import permissions as rule_permissions

from openedx_catalog.models import CourseRun
from openedx_learning.api import (
    delete_or_archive_competency_criteria_group,
    delete_or_archive_competency_criterion,
    get_course_competency_criteria,
)
from openedx_learning.applets.cbe import api as cbe_api
from openedx_learning.applets.cbe.data import (
    CompetencyTagData,
    CompetencyTaxonomyData,
    CourseCompetencyCriteria,
    CriteriaGroupData,
    CriterionData,
    TagLineageEntry,
)
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    MasteryStatus,
    RuleType,
    StudentCompetencyCriterionStatus,
)
from openedx_tagging.models import ObjectTag, Tag, Taxonomy

from .utils import (
    Branch,
    add_criterion,
    add_criterion_with_override,
    course_key_of,
    make_branch,
    make_competency_taxonomy,
    make_tag,
    usage_key,
)

pytestmark = pytest.mark.django_db

CHANGE_OBJECTTAG_OBJECTID = "oel_tagging.change_objecttag_objectid"

EMPTY_RESULT = CourseCompetencyCriteria(taxonomies=(), tags=(), groups=(), criteria=())


@pytest.fixture(autouse=True)
def _allow_object_level_tagging() -> Iterator[None]:
    """
    Let any user tag any object; restore the real rule afterwards.

    The archive API checks that its caller may tag the criterion's object, and openedx_tagging leaves
    that rule to openedx-platform.
    """
    original = rule_permissions[CHANGE_OBJECTTAG_OBJECTID]
    rules.set_perm(CHANGE_OBJECTTAG_OBJECTID, lambda _user, _object_id: True)
    yield
    rules.set_perm(CHANGE_OBJECTTAG_OBJECTID, original)


# ---------------------------------------------------------------------------------------------
# Helpers


def group_ids(result: CourseCompetencyCriteria) -> set[int]:
    """The ids of every group in `result`."""
    return {group.id for group in result.groups}


def criterion_ids(result: CourseCompetencyCriteria) -> set[int]:
    """The ids of every criterion in `result`."""
    return {criterion.id for criterion in result.criteria}


def assert_every_reference_resolves(result: CourseCompetencyCriteria) -> None:
    """
    Assert that a recipient can rebuild the taxonomy > competency > group > criterion nesting from `result`.

    Every parent a group or criterion names must itself be in the result, and so must every tag a group
    names and every taxonomy a tag names.
    """
    taxonomy_ids = {taxonomy.id for taxonomy in result.taxonomies}
    tag_ids = {tag.id for tag in result.tags}
    all_group_ids = group_ids(result)
    for tag in result.tags:
        assert tag.taxonomy_id in taxonomy_ids
    for group in result.groups:
        assert group.tag_id in tag_ids
        if group.parent_id is not None:
            assert group.parent_id in all_group_ids
    for criterion in result.criteria:
        assert criterion.group_id in all_group_ids


def record_learner_status(user, criterion: CompetencyCriterion, now: datetime) -> None:
    """Record that `user` has a competency status through `criterion`, so removing it archives instead of deleting."""
    StudentCompetencyCriterionStatus.objects.create(
        user=user, criterion=criterion, status_id=MasteryStatus.DEMONSTRATED, created=now, modified=now,
    )


def record_learner_status_on_every_criterion(user, now: datetime) -> None:
    """Give `user` a status through every criterion that exists, as the archive scenarios assume."""
    for criterion in CompetencyCriterion.objects.all():
        record_learner_status(user, criterion, now)


class TwoTaxonomyCourse(NamedTuple):
    """Course run X with a competency in each of two competency taxonomies."""

    nursing: CompetencyTaxonomy
    durable_skills: CompetencyTaxonomy
    root_cause_analysis: Tag
    communication: Tag
    root_cause_analysis_branch: Branch
    communication_branch: Branch


@pytest.fixture(name="two_taxonomy_course")
def _two_taxonomy_course(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> TwoTaxonomyCourse:
    """
    Course run X with criteria for "Root Cause Analysis" in taxonomy A (Nursing) and for "Communication" in
    taxonomy B (Durable Skills).
    """
    durable_skills = make_competency_taxonomy("Durable Skills")
    root_cause_analysis = make_tag(competency_taxonomy, "Root Cause Analysis")
    communication = make_tag(durable_skills, "Communication")
    return TwoTaxonomyCourse(
        nursing=competency_taxonomy,
        durable_skills=durable_skills,
        root_cause_analysis=root_cause_analysis,
        communication=communication,
        root_cause_analysis_branch=make_branch(root_cause_analysis, course_x, default_rule_profile, "rca_quiz"),
        communication_branch=make_branch(communication, course_x, default_rule_profile, "comm_quiz", "comm_essay"),
    )


def assert_only_communication_remains(result: CourseCompetencyCriteria, course: TwoTaxonomyCourse) -> None:
    """Assert that `result` holds "Communication" under its own taxonomy B, and nothing else."""
    branch = course.communication_branch
    assert [taxonomy.name for taxonomy in result.taxonomies] == ["Durable Skills"]
    assert [tag.value for tag in result.tags] == ["Communication"]
    assert group_ids(result) == {branch.root.id, branch.course_level.id, branch.bottom_tier.id}
    assert criterion_ids(result) == {criterion.id for criterion in branch.criteria}
    assert_every_reference_resolves(result)


# =============================================================================================
# Empty results
# =============================================================================================


def test_a_course_run_that_never_had_criteria_returns_an_empty_result(course_x: CourseRun) -> None:
    """AC9a: a course run in the catalog with no competency criteria gives four empty collections."""
    assert get_course_competency_criteria(course_key_of(course_x)) == EMPTY_RESULT


def test_a_well_formed_key_matching_no_course_run_returns_an_empty_result() -> None:
    """AC9b: a key that is not in the catalog is not an error; it has no criteria, like any other course run."""
    key_not_in_catalog = CourseKey.from_string("course-v1:Nowhere+Missing+Run")

    assert get_course_competency_criteria(key_not_in_catalog) == EMPTY_RESULT


def test_an_empty_result_costs_one_query(course_x: CourseRun, django_assert_num_queries) -> None:
    """N3, E17: finding no course-level group is the only query a course run without criteria costs."""
    with django_assert_num_queries(1):
        result = get_course_competency_criteria(course_key_of(course_x))

    assert result == EMPTY_RESULT


def test_a_key_differing_only_in_case_matches_nothing(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E4: the catalog's course key is case-sensitive, so another spelling of the case is another course run."""
    make_branch(tag, course_x, default_rule_profile, "quiz1")
    assert get_course_competency_criteria(course_key_of(course_x)).criteria
    other_case = CourseKey.from_string(str(course_key_of(course_x)).replace("Org1", "org1"))
    assert other_case != course_key_of(course_x)

    assert get_course_competency_criteria(other_case) == EMPTY_RESULT


def test_a_key_with_a_branch_matches_nothing(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E5, API side: the catalog holds the plain course key only, so a draft-branch key finds nothing."""
    make_branch(tag, course_x, default_rule_profile, "quiz1")
    assert get_course_competency_criteria(course_key_of(course_x)).criteria
    draft_key = CourseKey.from_string(f"{course_key_of(course_x)}+branch@draft-branch")

    assert get_course_competency_criteria(draft_key) == EMPTY_RESULT


# =============================================================================================
# What the result contains
# =============================================================================================


def test_the_result_can_be_reassembled_from_the_taxonomy_down_to_each_criterion(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    AC10: root, course-level and bottom-tier groups, two criteria, and the competency and taxonomy above
    them are all there, with every field the Progress tab displays.
    """
    communication = Tag.objects.create(taxonomy=competency_taxonomy, value="Communication", external_id="DS-4")
    branch = make_branch(communication, course_x, default_rule_profile, "quiz1", "essay1")
    # Distinct stored values in every group, so a field read from the wrong group shows up.
    for group, name, ordering, logic_operator in (
        (branch.root, "Communication (all courses)", 3, "OR"),
        (branch.course_level, "Communication in Python 100", 2, None),
        (branch.bottom_tier, "Quizzes and essays", 1, "AND"),
    ):
        CompetencyCriteriaGroup.objects.filter(id=group.id).update(
            name=name, ordering=ordering, logic_operator=logic_operator,
        )
    quiz, essay = branch.criteria

    result = get_course_competency_criteria(course_key_of(course_x))

    assert result.taxonomies == (CompetencyTaxonomyData(id=competency_taxonomy.id, name="Nursing"),)
    assert result.tags == (
        CompetencyTagData(
            id=communication.id,
            taxonomy_id=competency_taxonomy.id,
            value="Communication",
            external_id="DS-4",
            lineage=(TagLineageEntry(id=communication.id, value="Communication"),),
        ),
    )
    groups = {group.id: group for group in result.groups}
    assert set(groups) == {branch.root.id, branch.course_level.id, branch.bottom_tier.id}
    assert groups[branch.root.id] == CriteriaGroupData(
        id=branch.root.id, parent_id=None, tag_id=communication.id,
        name="Communication (all courses)", ordering=3, logic_operator="OR",
    )
    assert groups[branch.course_level.id] == CriteriaGroupData(
        id=branch.course_level.id, parent_id=branch.root.id, tag_id=communication.id,
        name="Communication in Python 100", ordering=2, logic_operator=None,
    )
    assert groups[branch.bottom_tier.id] == CriteriaGroupData(
        id=branch.bottom_tier.id, parent_id=branch.course_level.id, tag_id=communication.id,
        name="Quizzes and essays", ordering=1, logic_operator="AND",
    )
    assert list(result.criteria) == [
        CriterionData(
            id=quiz.id, group_id=branch.bottom_tier.id, object_id=usage_key(course_x, "quiz1"),
            rule_type="Grade", rule_payload=default_rule_profile.rule_payload,
        ),
        CriterionData(
            id=essay.id, group_id=branch.bottom_tier.id, object_id=usage_key(course_x, "essay1"),
            rule_type="Grade", rule_payload=default_rule_profile.rule_payload,
        ),
    ]
    assert_every_reference_resolves(result)


def test_only_course_xs_part_of_a_shared_competency_is_returned(
    course_x: CourseRun, course_y: CourseRun, competency_taxonomy: CompetencyTaxonomy,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC11: course Y's branch under the shared root, and a competency used only by course Y, stay out."""
    communication = make_tag(competency_taxonomy, "Communication")
    teamwork = make_tag(competency_taxonomy, "Teamwork")
    in_course_x = make_branch(communication, course_x, default_rule_profile, "x_quiz")
    in_course_y = make_branch(communication, course_y, default_rule_profile, "y_quiz")
    only_in_course_y = make_branch(teamwork, course_y, default_rule_profile, "y_project")
    assert in_course_x.root == in_course_y.root

    result = get_course_competency_criteria(course_key_of(course_x))

    assert [tag.value for tag in result.tags] == ["Communication"]
    assert group_ids(result) == {in_course_x.root.id, in_course_x.course_level.id, in_course_x.bottom_tier.id}
    assert criterion_ids(result) == {criterion.id for criterion in in_course_x.criteria}
    assert in_course_y.course_level.id not in group_ids(result)
    assert only_in_course_y.root.id not in group_ids(result)


def test_competencies_from_two_taxonomies_each_appear_under_their_own_taxonomy(
    course_x: CourseRun, two_taxonomy_course: TwoTaxonomyCourse,
) -> None:
    """AC12: both taxonomies are listed with their names, and each competency names only its own taxonomy."""
    result = get_course_competency_criteria(course_key_of(course_x))

    assert {taxonomy.id: taxonomy.name for taxonomy in result.taxonomies} == {
        two_taxonomy_course.nursing.id: "Nursing",
        two_taxonomy_course.durable_skills.id: "Durable Skills",
    }
    assert {tag.value: tag.taxonomy_id for tag in result.tags} == {
        "Root Cause Analysis": two_taxonomy_course.nursing.id,
        "Communication": two_taxonomy_course.durable_skills.id,
    }
    assert_every_reference_resolves(result)


def test_tags_from_a_taxonomy_that_is_not_a_competency_taxonomy_do_not_appear(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC12: subsections that also carry a course-topic tag still list only their competency."""
    course_topics = Taxonomy.objects.create(name="Course Topics")
    algebra = Tag.objects.create(taxonomy=course_topics, value="Algebra")
    branch = make_branch(tag, course_x, default_rule_profile, "quiz1")
    ObjectTag.objects.create(object_id=usage_key(course_x, "quiz1"), taxonomy=course_topics, tag=algebra)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert [taxonomy.name for taxonomy in result.taxonomies] == ["Nursing"]
    assert [competency.value for competency in result.tags] == ["Writing Poetry"]
    assert criterion_ids(result) == {criterion.id for criterion in branch.criteria}


def test_a_criterion_in_a_taxonomy_that_is_not_a_competency_taxonomy_is_left_out(
    course_x: CourseRun, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E19: groups and criteria written for a plain taxonomy's tag are not competency criteria."""
    course_topics = Taxonomy.objects.create(name="Course Topics")
    algebra = Tag.objects.create(taxonomy=course_topics, value="Algebra")
    make_branch(algebra, course_x, default_rule_profile, "quiz1")

    assert get_course_competency_criteria(course_key_of(course_x)) == EMPTY_RESULT


def test_each_competency_carries_its_lineage_from_the_top_of_its_taxonomy(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC13: "Root Cause Analysis" lists its two ancestors root-first, and "Communication" lists only itself."""
    problem_solving = make_tag(competency_taxonomy, "Problem Solving")
    problem_definition = make_tag(competency_taxonomy, "Problem Definition", problem_solving)
    root_cause_analysis = make_tag(competency_taxonomy, "Root Cause Analysis", problem_definition)
    communication = make_tag(competency_taxonomy, "Communication")
    make_branch(root_cause_analysis, course_x, default_rule_profile, "rca_quiz")
    make_branch(communication, course_x, default_rule_profile, "comm_quiz")

    result = get_course_competency_criteria(course_key_of(course_x))

    lineages = {tag.value: tag.lineage for tag in result.tags}
    assert lineages["Root Cause Analysis"] == (
        TagLineageEntry(id=problem_solving.id, value="Problem Solving"),
        TagLineageEntry(id=problem_definition.id, value="Problem Definition"),
        TagLineageEntry(id=root_cause_analysis.id, value="Root Cause Analysis"),
    )
    assert lineages["Communication"] == (TagLineageEntry(id=communication.id, value="Communication"),)


def test_an_archived_ancestor_tag_stays_in_the_lineage(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E13: archiving a parent tag does not archive its children, so the competency and its breadcrumb stay whole."""
    problem_solving = make_tag(competency_taxonomy, "Problem Solving")
    root_cause_analysis = make_tag(competency_taxonomy, "Root Cause Analysis", problem_solving)
    make_branch(root_cause_analysis, course_x, default_rule_profile, "rca_quiz")
    Tag.objects.filter(id=problem_solving.id).update(archived=True)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert [tag.value for tag in result.tags] == ["Root Cause Analysis"]
    assert result.tags[0].lineage == (
        TagLineageEntry(id=problem_solving.id, value="Problem Solving"),
        TagLineageEntry(id=root_cause_analysis.id, value="Root Cause Analysis"),
    )


def test_an_ancestor_value_in_another_taxonomy_does_not_leak_into_a_lineage(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E14: two taxonomies each have a "Problem Solving" tag, and each competency lists its own taxonomy's."""
    durable_skills = make_competency_taxonomy("Durable Skills")
    nursing_problem_solving = make_tag(competency_taxonomy, "Problem Solving")
    nursing_root_cause_analysis = make_tag(competency_taxonomy, "Root Cause Analysis", nursing_problem_solving)
    durable_problem_solving = make_tag(durable_skills, "Problem Solving")
    durable_estimation = make_tag(durable_skills, "Estimation", durable_problem_solving)
    make_branch(nursing_root_cause_analysis, course_x, default_rule_profile, "rca_quiz")
    make_branch(durable_estimation, course_x, default_rule_profile, "estimation_quiz")

    result = get_course_competency_criteria(course_key_of(course_x))

    lineage_ids = {tag.value: [entry.id for entry in tag.lineage] for tag in result.tags}
    assert lineage_ids == {
        "Root Cause Analysis": [nursing_problem_solving.id, nursing_root_cause_analysis.id],
        "Estimation": [durable_problem_solving.id, durable_estimation.id],
    }


# =============================================================================================
# The effective rule
# =============================================================================================


def test_a_criterion_without_an_override_carries_the_system_default_rule_profiles_rule(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC14: the criterion reports the rule of the system default profile it is assigned."""
    # The migration seeds 0.8; the ticket's scenario uses 75 percent, stored as the fraction 0.75.
    default_rule_profile.rule_payload = {"op": "gte", "value": 0.75, "scale": "percent"}
    default_rule_profile.save()
    make_branch(tag, course_x, default_rule_profile, "quiz1")

    result = get_course_competency_criteria(course_key_of(course_x))

    (criterion,) = result.criteria
    assert criterion.rule_type == RuleType.GRADE
    assert criterion.rule_payload == {"op": "gte", "value": 0.75, "scale": "percent"}


def test_a_criterions_own_override_takes_the_place_of_the_rule_profile(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC15: a criterion with its own 82 percent rule reports it, while the default profile says 75 percent."""
    default_rule_profile.rule_payload = {"op": "gte", "value": 0.75, "scale": "percent"}
    default_rule_profile.save()
    branch = make_branch(tag, course_x, default_rule_profile)
    add_criterion_with_override(
        branch.bottom_tier, course_x, "essay1", {"op": "gte", "value": 0.82, "scale": "percent"},
    )

    result = get_course_competency_criteria(course_key_of(course_x))

    (criterion,) = result.criteria
    assert criterion.rule_type == RuleType.GRADE
    assert criterion.rule_payload == {"op": "gte", "value": 0.82, "scale": "percent"}


def test_rule_payloads_are_returned_as_stored(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, tag: Tag,
) -> None:
    """N4: the grade value stays the stored fraction, for a profile's rule and for an override alike."""
    profile_payload = {"op": "lte", "value": 0.5, "scale": "percent"}
    override_payload = {"op": "eq", "value": 1.0, "scale": "percent"}
    profile = CompetencyRuleProfile.objects.create(
        competency_taxonomy=competency_taxonomy, rule_type=RuleType.GRADE, rule_payload=profile_payload,
    )
    branch = make_branch(tag, course_x, profile, "profile_quiz")
    with_override = add_criterion_with_override(branch.bottom_tier, course_x, "override_quiz", override_payload)

    result = get_course_competency_criteria(course_key_of(course_x))

    payloads = {criterion.id: criterion.rule_payload for criterion in result.criteria}
    assert payloads == {branch.criteria[0].id: profile_payload, with_override.id: override_payload}


def test_a_criterion_on_an_archived_rule_profile_is_still_returned_with_that_profiles_rule(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, tag: Tag,
) -> None:
    """E12: archiving a profile hides it from new assignments, but criteria already on it keep its rule."""
    payload = {"op": "gte", "value": 0.6, "scale": "percent"}
    profile = CompetencyRuleProfile.objects.create(
        competency_taxonomy=competency_taxonomy, rule_type=RuleType.GRADE, rule_payload=payload,
    )
    branch = make_branch(tag, course_x, profile, "quiz1")
    profile.archived = True
    profile.save()

    result = get_course_competency_criteria(course_key_of(course_x))

    assert criterion_ids(result) == {branch.criteria[0].id}
    assert result.criteria[0].rule_payload == payload


# =============================================================================================
# Subsections a learner cannot see, and who is asking
# =============================================================================================


def test_criteria_on_subsections_a_learner_cannot_see_are_still_returned(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """
    AC21, core side: this library has no notion of a subsection's visibility, so every criterion comes back
    with its usage key, including one on a subsection that has since been deleted from the course.
    """
    subsections = ("released", "not_yet_released", "hidden_from_learners", "content_group_only", "deleted")
    make_branch(tag, course_x, default_rule_profile, *subsections)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert {criterion.object_id for criterion in result.criteria} == {
        usage_key(course_x, subsection) for subsection in subsections
    }


def test_the_function_takes_a_course_key_and_checks_no_permission() -> None:
    """N13: the function knows no user, so the caller enforces who may read, and #738 can reuse it."""
    assert list(inspect.signature(get_course_competency_criteria).parameters) == ["course_key"]


def test_the_function_is_part_of_the_public_api_and_marked_unstable() -> None:
    """N13: it is exported from the applet's API, which is what openedx_learning.api re-exports."""
    assert "get_course_competency_criteria" in cbe_api.__all__
    assert "UNSTABLE" in (get_course_competency_criteria.__doc__ or "")


def test_the_result_is_an_immutable_value_object_made_of_tuples(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """The design's value objects: frozen dataclasses, with a tuple wherever there is a collection."""
    make_branch(tag, course_x, default_rule_profile, "quiz1")

    result = get_course_competency_criteria(course_key_of(course_x))

    assert isinstance(result, CourseCompetencyCriteria)
    assert isinstance(result.taxonomies, tuple) and isinstance(result.taxonomies[0], CompetencyTaxonomyData)
    assert isinstance(result.tags, tuple) and isinstance(result.tags[0], CompetencyTagData)
    assert isinstance(result.tags[0].lineage, tuple) and isinstance(result.tags[0].lineage[0], TagLineageEntry)
    assert isinstance(result.groups, tuple) and isinstance(result.groups[0], CriteriaGroupData)
    assert isinstance(result.criteria, tuple) and isinstance(result.criteria[0], CriterionData)
    attribute_name = "criteria"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(result, attribute_name, ())


# =============================================================================================
# Order
# =============================================================================================


def test_taxonomies_are_listed_by_id(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC16, order: the taxonomy created first comes first, whichever one the first criterion belongs to."""
    durable_skills = make_competency_taxonomy("Durable Skills")
    make_branch(make_tag(durable_skills, "Communication"), course_x, default_rule_profile, "comm_quiz")
    make_branch(make_tag(competency_taxonomy, "Root Cause Analysis"), course_x, default_rule_profile, "rca_quiz")
    assert competency_taxonomy.id < durable_skills.id

    result = get_course_competency_criteria(course_key_of(course_x))

    assert [taxonomy.name for taxonomy in result.taxonomies] == ["Nursing", "Durable Skills"]


def test_tags_are_listed_by_id_not_by_name(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC16, order: the tag created first comes first, though its name sorts last and its criteria came last."""
    zeta = make_tag(competency_taxonomy, "Zeta")
    alpha = make_tag(competency_taxonomy, "Alpha")
    make_branch(alpha, course_x, default_rule_profile, "alpha_quiz")
    make_branch(zeta, course_x, default_rule_profile, "zeta_quiz")

    result = get_course_competency_criteria(course_key_of(course_x))

    assert [tag.value for tag in result.tags] == ["Zeta", "Alpha"]


def test_criteria_are_listed_by_id_not_by_group_order_or_usage_key(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC16, order: the first criterion created comes first, though its group and its usage key sort last."""
    first = make_branch(tag, course_x, default_rule_profile, "z_quiz")
    second = make_branch(tag, course_x, default_rule_profile, "a_quiz")
    CompetencyCriteriaGroup.objects.filter(id=first.bottom_tier.id).update(ordering=5)
    CompetencyCriteriaGroup.objects.filter(id=second.bottom_tier.id).update(ordering=1)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert [criterion.id for criterion in result.criteria] == [first.criteria[0].id, second.criteria[0].id]


def test_groups_are_listed_level_by_level_so_every_group_follows_its_parent(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC16, order: all roots, then all course-level groups, then all bottom-tier groups."""
    communication = make_branch(
        make_tag(competency_taxonomy, "Communication"), course_x, default_rule_profile, "comm_quiz",
    )
    teamwork = make_branch(make_tag(competency_taxonomy, "Teamwork"), course_x, default_rule_profile, "team_quiz")
    # By id alone, Communication's three groups would all come before Teamwork's root.
    assert communication.bottom_tier.id < teamwork.root.id

    result = get_course_competency_criteria(course_key_of(course_x))

    assert [group.id for group in result.groups] == [
        communication.root.id,
        teamwork.root.id,
        communication.course_level.id,
        teamwork.course_level.id,
        communication.bottom_tier.id,
        teamwork.bottom_tier.id,
    ]


def test_sibling_groups_are_listed_by_their_stored_ordering_not_by_id(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E16: the stored ordering of two bottom-tier groups is the reverse of their id order, and it wins."""
    earlier_id = make_branch(tag, course_x, default_rule_profile, "quiz1")
    later_id = make_branch(tag, course_x, default_rule_profile, "quiz2")
    CompetencyCriteriaGroup.objects.filter(id=earlier_id.bottom_tier.id).update(ordering=5)
    CompetencyCriteriaGroup.objects.filter(id=later_id.bottom_tier.id).update(ordering=1)

    result = get_course_competency_criteria(course_key_of(course_x))

    bottom_tier_ids = [group.id for group in result.groups if group.parent_id == earlier_id.course_level.id]
    assert bottom_tier_ids == [later_id.bottom_tier.id, earlier_id.bottom_tier.id]


def test_sibling_groups_with_equal_ordering_are_listed_by_id(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E16: two bottom-tier groups that share an ordering keep the order they were created in."""
    first = make_branch(tag, course_x, default_rule_profile, "quiz1")
    second = make_branch(tag, course_x, default_rule_profile, "quiz2")
    CompetencyCriteriaGroup.objects.filter(id__in=[first.bottom_tier.id, second.bottom_tier.id]).update(ordering=4)

    result = get_course_competency_criteria(course_key_of(course_x))

    bottom_tier_ids = [group.id for group in result.groups if group.parent_id == first.course_level.id]
    assert bottom_tier_ids == [first.bottom_tier.id, second.bottom_tier.id]


def test_root_groups_are_listed_by_their_stored_ordering_across_competencies(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E16: the ordering applies across a whole level, so one competency's root can come before another's."""
    communication = make_branch(
        make_tag(competency_taxonomy, "Communication"), course_x, default_rule_profile, "comm_quiz",
    )
    teamwork = make_branch(make_tag(competency_taxonomy, "Teamwork"), course_x, default_rule_profile, "team_quiz")
    CompetencyCriteriaGroup.objects.filter(id=communication.root.id).update(ordering=2)
    CompetencyCriteriaGroup.objects.filter(id=teamwork.root.id).update(ordering=1)

    result = get_course_competency_criteria(course_key_of(course_x))

    root_ids = [group.id for group in result.groups if group.parent_id is None]
    assert root_ids == [teamwork.root.id, communication.root.id]


# =============================================================================================
# Archived and disabled records, left out through the real archive API
# =============================================================================================


def test_an_archived_criterion_is_left_out_while_its_sibling_stays(
    user, now: datetime, course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC17: after #674 archives one of two criteria in a bottom-tier group, only the other is returned."""
    branch = make_branch(tag, course_x, default_rule_profile, "quiz1", "quiz2")
    archived, sibling = branch.criteria
    record_learner_status_on_every_criterion(user, now)
    assert delete_or_archive_competency_criterion(archived.id, user).archived is True

    result = get_course_competency_criteria(course_key_of(course_x))

    assert criterion_ids(result) == {sibling.id}
    assert branch.bottom_tier.id in group_ids(result)


def test_archiving_a_bottom_tier_group_takes_its_subtree_out_while_the_rest_stays(
    user, now: datetime, course_x: CourseRun, two_taxonomy_course: TwoTaxonomyCourse,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC18a: the archived group and its criteria are gone, while its sibling group and the other competency stay."""
    rca = two_taxonomy_course.root_cause_analysis_branch
    communication = two_taxonomy_course.communication_branch
    second_rca_group = make_branch(two_taxonomy_course.root_cause_analysis, course_x, default_rule_profile, "rca_essay")
    record_learner_status_on_every_criterion(user, now)
    assert delete_or_archive_competency_criteria_group(rca.bottom_tier.id, user).archived is True

    result = get_course_competency_criteria(course_key_of(course_x))

    assert group_ids(result) == {
        second_rca_group.root.id, second_rca_group.course_level.id, second_rca_group.bottom_tier.id,
        communication.root.id, communication.course_level.id, communication.bottom_tier.id,
    }
    assert criterion_ids(result) == (
        {criterion.id for criterion in second_rca_group.criteria}
        | {criterion.id for criterion in communication.criteria}
    )
    assert_every_reference_resolves(result)


def test_archiving_a_course_level_group_takes_its_whole_branch_and_competency_out(
    user, now: datetime, course_x: CourseRun, two_taxonomy_course: TwoTaxonomyCourse,
) -> None:
    """AC18b: the archived course-level group, its subtree and its competency are gone; the other competency stays."""
    rca = two_taxonomy_course.root_cause_analysis_branch
    record_learner_status_on_every_criterion(user, now)
    assert delete_or_archive_competency_criteria_group(rca.course_level.id, user).archived is True

    result = get_course_competency_criteria(course_key_of(course_x))

    assert_only_communication_remains(result, two_taxonomy_course)


@pytest.mark.usefixtures("two_taxonomy_course")
def test_a_course_run_whose_criteria_are_all_archived_returns_an_empty_result(
    user, now: datetime, course_x: CourseRun,
) -> None:
    """AC20a: once #674 has archived every criterion, the course run reads as if it never had any."""
    record_learner_status_on_every_criterion(user, now)
    for criterion in CompetencyCriterion.objects.all():
        assert delete_or_archive_competency_criterion(criterion.id, user).archived is True

    assert get_course_competency_criteria(course_key_of(course_x)) == EMPTY_RESULT


# =============================================================================================
# Archived and disabled records, written directly
# =============================================================================================


def test_a_competency_whose_tag_is_archived_is_left_out_with_everything_beneath_it(
    course_x: CourseRun, two_taxonomy_course: TwoTaxonomyCourse,
) -> None:
    """AC19a"""
    Tag.objects.filter(id=two_taxonomy_course.root_cause_analysis.id).update(archived=True)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert_only_communication_remains(result, two_taxonomy_course)


def test_a_competency_whose_taxonomy_is_archived_is_left_out_with_everything_beneath_it(
    course_x: CourseRun, two_taxonomy_course: TwoTaxonomyCourse,
) -> None:
    """AC19b: the taxonomy is archived and none of its tags is."""
    Taxonomy.objects.filter(id=two_taxonomy_course.nursing.id).update(archived=True)
    assert not Tag.objects.filter(taxonomy=two_taxonomy_course.nursing, archived=True).exists()

    result = get_course_competency_criteria(course_key_of(course_x))

    assert_only_communication_remains(result, two_taxonomy_course)


def test_a_competency_whose_taxonomy_is_disabled_is_left_out_with_everything_beneath_it(
    course_x: CourseRun, two_taxonomy_course: TwoTaxonomyCourse,
) -> None:
    """AC19c"""
    Taxonomy.objects.filter(id=two_taxonomy_course.nursing.id).update(enabled=False)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert_only_communication_remains(result, two_taxonomy_course)


def test_a_course_run_whose_only_taxonomy_is_disabled_returns_an_empty_result(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, tag: Tag,
    default_rule_profile: CompetencyRuleProfile,
) -> None:
    """AC20b"""
    make_branch(tag, course_x, default_rule_profile, "quiz1")
    Taxonomy.objects.filter(id=competency_taxonomy.id).update(enabled=False)

    assert get_course_competency_criteria(course_key_of(course_x)) == EMPTY_RESULT


def test_a_live_course_level_group_under_an_archived_root_is_left_out(
    course_x: CourseRun, two_taxonomy_course: TwoTaxonomyCourse,
) -> None:
    """E8: the API archives a root only after its branches, but a write outside it could leave this state."""
    CompetencyCriteriaGroup.objects.filter(id=two_taxonomy_course.root_cause_analysis_branch.root.id).update(
        archived=True,
    )

    result = get_course_competency_criteria(course_key_of(course_x))

    assert_only_communication_remains(result, two_taxonomy_course)


def test_live_groups_below_an_archived_bottom_tier_group_are_left_out(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E9: the API archives a whole subtree at once, but a write outside it could leave live children below."""
    archived_group = make_branch(tag, course_x, default_rule_profile, "quiz1")
    live_group = make_branch(tag, course_x, default_rule_profile, "quiz2")
    below_archived = CompetencyCriteriaGroup.objects.create(tag=tag, parent=archived_group.bottom_tier)
    criterion_below = add_criterion(below_archived, course_x, default_rule_profile, "quiz3")
    CompetencyCriteriaGroup.objects.filter(id=archived_group.bottom_tier.id).update(archived=True)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert below_archived.id not in group_ids(result)
    assert criterion_below.id not in criterion_ids(result)
    assert group_ids(result) == {live_group.root.id, live_group.course_level.id, live_group.bottom_tier.id}
    assert_every_reference_resolves(result)


def test_a_criterion_whose_object_tag_is_archived_is_left_out_and_its_emptied_group_with_it(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E10: nothing archives an ObjectTag yet, but archived associations behave as gone from ordinary reads."""
    emptied = make_branch(tag, course_x, default_rule_profile, "quiz1")
    remaining = make_branch(tag, course_x, default_rule_profile, "quiz2")
    ObjectTag.objects.filter(id=emptied.criteria[0].object_tag_id).update(archived=True)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert criterion_ids(result) == {remaining.criteria[0].id}
    assert emptied.bottom_tier.id not in group_ids(result)
    assert remaining.bottom_tier.id in group_ids(result)


def test_a_live_group_with_no_live_criteria_beneath_it_is_left_out(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E11: an empty bottom-tier group next to one that holds a criterion is not worth showing."""
    with_criterion = make_branch(tag, course_x, default_rule_profile, "quiz1")
    empty = make_branch(tag, course_x, default_rule_profile)

    result = get_course_competency_criteria(course_key_of(course_x))

    assert empty.bottom_tier.id not in group_ids(result)
    assert group_ids(result) == {with_criterion.root.id, with_criterion.course_level.id, with_criterion.bottom_tier.id}


def test_a_competency_with_no_live_criteria_beneath_it_is_absent(
    course_x: CourseRun, two_taxonomy_course: TwoTaxonomyCourse,
) -> None:
    """E11: a competency whose groups hold no criteria disappears from the result, with its tag and taxonomy."""
    CompetencyCriterion.objects.filter(group=two_taxonomy_course.root_cause_analysis_branch.bottom_tier).delete()

    result = get_course_competency_criteria(course_key_of(course_x))

    assert_only_communication_remains(result, two_taxonomy_course)


def test_a_group_below_a_bottom_tier_group_is_returned_with_consistent_parent_ids(
    course_x: CourseRun, tag: Tag, default_rule_profile: CompetencyRuleProfile,
) -> None:
    """E15: the endpoint returns the course's whole subtree at any depth, so a fourth level works already."""
    branch = make_branch(tag, course_x, default_rule_profile)
    fourth_level = CompetencyCriteriaGroup.objects.create(tag=tag, parent=branch.bottom_tier)
    criterion = add_criterion(fourth_level, course_x, default_rule_profile, "quiz1")

    result = get_course_competency_criteria(course_key_of(course_x))

    assert group_ids(result) == {branch.root.id, branch.course_level.id, branch.bottom_tier.id, fourth_level.id}
    parent_ids = {group.id: group.parent_id for group in result.groups}
    assert parent_ids[fourth_level.id] == branch.bottom_tier.id
    assert [(c.id, c.group_id) for c in result.criteria] == [(criterion.id, fourth_level.id)]
    assert_every_reference_resolves(result)


# =============================================================================================
# Query count
# =============================================================================================


def test_a_populated_course_costs_five_queries_however_many_competencies_it_has(
    course_x: CourseRun, competency_taxonomy: CompetencyTaxonomy, default_rule_profile: CompetencyRuleProfile,
    django_assert_num_queries,
) -> None:
    """
    E17: course-level groups, bottom-tier groups, the empty level below them, criteria, and the ancestor tags.
    A third competency, in a third taxonomy, adds rows but no queries.
    """
    problem_solving = make_tag(competency_taxonomy, "Problem Solving")
    root_cause_analysis = make_tag(competency_taxonomy, "Root Cause Analysis", problem_solving)
    communication = make_tag(make_competency_taxonomy("Durable Skills"), "Communication")
    make_branch(root_cause_analysis, course_x, default_rule_profile, "rca_quiz")
    make_branch(communication, course_x, default_rule_profile, "comm_quiz")

    with django_assert_num_queries(5):
        two_competencies = get_course_competency_criteria(course_key_of(course_x))
    assert len(two_competencies.tags) == 2

    teamwork_skills = make_competency_taxonomy("Teamwork Skills")
    collaboration = make_tag(teamwork_skills, "Collaboration")
    shared_work = make_tag(teamwork_skills, "Shared Work", collaboration)
    make_branch(shared_work, course_x, default_rule_profile, "shared_quiz")

    with django_assert_num_queries(5):
        three_competencies = get_course_competency_criteria(course_key_of(course_x))
    assert len(three_competencies.tags) == 3
