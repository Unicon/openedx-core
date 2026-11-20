"""Shared test helpers for the CBE test modules."""
from typing import NamedTuple

from organizations.models import Organization

from openedx_catalog.models import CatalogCourse, CourseRun
from openedx_learning.applets.cbe.api import _create_leaf_group
from openedx_learning.models import (
    CompetencyCriteriaGroup,
    CompetencyCriterion,
    CompetencyRuleProfile,
    CompetencyTaxonomy,
    RuleType,
)
from openedx_tagging.models import ObjectTag, Tag


def usage_key(course_run: CourseRun, block_id: str) -> str:
    """Build a gradeable-subsection-shaped usage key string under `course_run`."""
    key = course_run.course_key
    assert key is not None
    return f"block-v1:{key.org}+{key.course}+{key.run}+type@sequential+block@{block_id}"


def make_leaf_criterion(
    tag: Tag, course_run: CourseRun, default_rule_profile: CompetencyRuleProfile, block_id: str,
) -> CompetencyCriterion:
    """Create a leaf group and one live CompetencyCriterion for `tag` in `course_run`."""
    leaf = _create_leaf_group(tag, course_run)
    object_tag = ObjectTag.objects.create(object_id=usage_key(course_run, block_id), taxonomy=tag.taxonomy, tag=tag)
    return CompetencyCriterion.objects.create(group=leaf, object_tag=object_tag, rule_profile=default_rule_profile)


def make_course_run(organization: Organization, course_code: str, run_code: str) -> CourseRun:
    """Create a CourseRun distinct from the `course_run` fixture, for the different-course tests."""
    catalog_course = CatalogCourse.objects.create(org=organization, course_code=course_code)
    return CourseRun.objects.create(catalog_course=catalog_course, run_code=run_code)


def make_tag(taxonomy: CompetencyTaxonomy, value: str, parent: Tag | None = None) -> Tag:
    """Create a Tag on `taxonomy`, optionally under `parent`, for taxonomy-hierarchy tests."""
    return Tag.objects.create(taxonomy=taxonomy, value=value, parent=parent)


def make_skill_family(taxonomy: CompetencyTaxonomy) -> tuple[Tag, Tag, Tag, Tag]:
    """
    Create "Cognitive Skills" > "Critical Thinking" > "Inference", plus "Analysis" as a sibling of "Inference".
    """
    cognitive_skills = Tag.objects.create(taxonomy=taxonomy, value="Cognitive Skills")
    critical_thinking = Tag.objects.create(taxonomy=taxonomy, value="Critical Thinking", parent=cognitive_skills)
    inference = Tag.objects.create(taxonomy=taxonomy, value="Inference", parent=critical_thinking)
    analysis = Tag.objects.create(taxonomy=taxonomy, value="Analysis", parent=critical_thinking)
    return cognitive_skills, critical_thinking, inference, analysis


def make_competency_taxonomy(name: str) -> CompetencyTaxonomy:
    """Create a CompetencyTaxonomy called `name`, distinct from the `competency_taxonomy` fixture."""
    return CompetencyTaxonomy.objects.create(name=name, export_id=name.lower().replace(" ", "-"))


class Branch(NamedTuple):
    """One competency's groups for one course run, from the shared root down to a bottom-tier group."""

    root: CompetencyCriteriaGroup
    course_level: CompetencyCriteriaGroup
    bottom_tier: CompetencyCriteriaGroup
    criteria: list[CompetencyCriterion]


def make_branch(tag: Tag, course_run: CourseRun, rule_profile: CompetencyRuleProfile, *block_ids: str) -> Branch:
    """
    Create a bottom-tier group for `tag` in `course_run`, with one criterion per entry of `block_ids`.

    The root and the course-level group are reused when they already exist, so calling this twice for the
    same tag and course run gives two bottom-tier groups under the same course-level group. Every criterion
    uses `rule_profile`. With no `block_ids` the bottom-tier group is empty.
    """
    bottom_tier = _create_leaf_group(tag, course_run)
    course_level = bottom_tier.parent
    assert course_level is not None
    root = course_level.parent
    assert root is not None
    criteria = [add_criterion(bottom_tier, course_run, rule_profile, block_id) for block_id in block_ids]
    return Branch(root=root, course_level=course_level, bottom_tier=bottom_tier, criteria=criteria)


def add_criterion(
    group: CompetencyCriteriaGroup, course_run: CourseRun, rule_profile: CompetencyRuleProfile, block_id: str,
) -> CompetencyCriterion:
    """Create a criterion in `group` on the subsection `block_id` of `course_run`, using `rule_profile`'s rule."""
    object_tag = ObjectTag.objects.create(
        object_id=usage_key(course_run, block_id), taxonomy=group.tag.taxonomy, tag=group.tag,
    )
    return CompetencyCriterion.objects.create(group=group, object_tag=object_tag, rule_profile=rule_profile)


def add_criterion_with_override(
    group: CompetencyCriteriaGroup, course_run: CourseRun, block_id: str, rule_payload: dict,
) -> CompetencyCriterion:
    """Create a criterion in `group` on the subsection `block_id` with its own Grade rule and no rule profile."""
    object_tag = ObjectTag.objects.create(
        object_id=usage_key(course_run, block_id), taxonomy=group.tag.taxonomy, tag=group.tag,
    )
    return CompetencyCriterion.objects.create(
        group=group,
        object_tag=object_tag,
        rule_type_override=RuleType.GRADE,
        rule_payload_override=rule_payload,
    )
