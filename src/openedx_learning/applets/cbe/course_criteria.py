"""
The read behind the learner-facing list of a course run's competency criteria.

The selection runs in steps, each a function below: find the course-level groups, walk down to every
group beneath them, find the criteria on those groups, prune the groups that hold no criterion, and
add each competency's lineage. Assembly into value objects comes last, so no step mixes the two.

A competency has one root group shared by every course run. A course run's part of it, its "course
branch", is its course-level group and every group beneath that.
"""
from __future__ import annotations

from collections.abc import Iterable
from itertools import chain

from opaque_keys.edx.keys import CourseKey
from opaque_keys.edx.locator import CourseLocator

from openedx_tagging.models import Tag, Taxonomy

from .data import (
    CompetencyTagData,
    CompetencyTaxonomyData,
    CourseCompetencyCriteria,
    CriteriaGroupData,
    CriterionData,
    TagLineageEntry,
)
from .models import CompetencyCriteriaGroup, CompetencyCriterion
from .rule_payloads import effective_rule

__all__ = [
    "get_course_competency_criteria",
]

_EMPTY_RESULT = CourseCompetencyCriteria(taxonomies=(), tags=(), groups=(), criteria=())


def get_course_competency_criteria(course_key: CourseKey) -> CourseCompetencyCriteria:
    """
    Return the competency criteria of the course run ``course_key``, with the groups, competencies and
    taxonomies needed to place each criterion.

    UNSTABLE: the CBE criteria APIs are incomplete, so this function and its result may change without a
    deprecation cycle.

    This performs no authorization. The caller must first check that its user may read the course run,
    which is what ``VIEW_COURSE_COMPETENCY_CRITERIA`` in ``openedx_learning.rules`` is for.

    Left out, with everything beneath them: archived groups and criteria, criteria whose object tag is
    archived, competencies whose tag or taxonomy is archived, and competencies of a disabled taxonomy or a
    non-competency taxonomy. A group, competency or taxonomy with no returned criterion beneath it is left
    out too. A course run with nothing to return, or one the catalog does not know, gives an empty result.

    A key that carries a branch or a version matches nothing.

    ``taxonomies``, ``tags`` and ``criteria`` are ordered by id. ``groups`` come level by level, roots
    first, then the course-level groups, then each deeper level, by ``(ordering, id)`` within a level, so
    every group follows its parent. A tag's ``lineage`` runs from the top of its taxonomy down to the tag.

    A course-level group is found only directly under a root, the one shape the authoring API creates.
    That keeps every returned group's parent in the result, so a client can rebuild the nesting from it.
    """
    if _carries_branch_or_version(course_key):
        return _EMPTY_RESULT
    course_level_groups = _find_course_level_groups(course_key)
    if not course_level_groups:
        return _EMPTY_RESULT
    root_groups = _roots_above(course_level_groups)
    course_branch_levels = [course_level_groups, *_find_levels_below(course_level_groups)]
    criteria = _find_criteria(course_branch_levels)

    groups_by_level = [root_groups, *course_branch_levels]
    kept_group_ids = _ids_of_groups_with_criteria_beneath(criteria, groups_by_level)
    kept_groups_by_level = [_only_kept(level, kept_group_ids) for level in groups_by_level]
    tags = _tags_of(_only_kept(course_level_groups, kept_group_ids))

    return CourseCompetencyCriteria(
        taxonomies=_taxonomy_data(tags),
        tags=_tag_data(tags),
        groups=tuple(_group_data(group) for group in chain.from_iterable(kept_groups_by_level)),
        criteria=tuple(_criterion_data(criterion) for criterion in criteria),
    )


# Selection


def _carries_branch_or_version(course_key: CourseKey) -> bool:
    """Say whether ``course_key`` has a branch or a version, which the ``course_key`` field strips when comparing."""
    return isinstance(course_key, CourseLocator) and (
        course_key.branch is not None or course_key.version_guid is not None
    )


def _find_course_level_groups(course_key: CourseKey) -> list[CompetencyCriteriaGroup]:
    """Find the course run's live course-level groups whose competency is live and in an enabled competency taxonomy."""
    return list(
        CompetencyCriteriaGroup.objects.filter(
            course__course_key=course_key,
            archived=False,
            parent__archived=False,
            parent__parent__isnull=True,
            tag__archived=False,
            tag__taxonomy__archived=False,
            tag__taxonomy__enabled=True,
            tag__taxonomy__competencytaxonomy__isnull=False,
        )
        .select_related("parent", "tag__taxonomy")
        .order_by("ordering", "id")
    )


def _roots_above(course_level_groups: list[CompetencyCriteriaGroup]) -> list[CompetencyCriteriaGroup]:
    """Return the roots the course-level groups hang from; a root is shared by every course run of its competency."""
    roots_by_id = {root.id: root for root in map(_root_of, course_level_groups)}
    return sorted(roots_by_id.values(), key=_ordering_then_id)


def _root_of(course_level_group: CompetencyCriteriaGroup) -> CompetencyCriteriaGroup:
    # The course-level query only returns groups that have a parent; this narrows the type for mypy.
    assert course_level_group.parent is not None
    return course_level_group.parent


def _find_levels_below(course_level_groups: list[CompetencyCriteriaGroup]) -> list[list[CompetencyCriteriaGroup]]:
    """
    Find the live groups beneath the course-level groups, one list per level, top level first.

    A group is reached only through live parents, so an archived group takes its whole subtree with it.
    """
    levels: list[list[CompetencyCriteriaGroup]] = []
    parents = course_level_groups
    while True:
        children = list(
            CompetencyCriteriaGroup.objects.filter(
                parent_id__in=[group.id for group in parents], archived=False,
            ).order_by("ordering", "id")
        )
        if not children:
            return levels
        levels.append(children)
        parents = children


def _find_criteria(course_branch_levels: list[list[CompetencyCriteriaGroup]]) -> list[CompetencyCriterion]:
    """
    Find the live criteria of the course branch's groups.

    Each criterion's object tag (its usage key) and rule profile (its rule) are loaded with it.
    """
    group_ids = [group.id for group in chain.from_iterable(course_branch_levels)]
    return list(
        CompetencyCriterion.objects.filter(group_id__in=group_ids, archived=False, object_tag__archived=False)
        .select_related("object_tag", "rule_profile")
        .order_by("id")
    )


# Pruning


def _ids_of_groups_with_criteria_beneath(
    criteria: list[CompetencyCriterion], groups_by_level: list[list[CompetencyCriteriaGroup]],
) -> set[int]:
    """Return the ids of the groups that hold a criterion or have one somewhere beneath them."""
    parent_id_by_group_id = {group.id: group.parent_id for group in chain.from_iterable(groups_by_level)}
    group_ids: set[int] = set()
    for criterion in criteria:
        group_id: int | None = criterion.group_id
        while group_id is not None and group_id not in group_ids:
            group_ids.add(group_id)
            group_id = parent_id_by_group_id[group_id]
    return group_ids


def _only_kept(groups: Iterable[CompetencyCriteriaGroup], kept_group_ids: set[int]) -> list[CompetencyCriteriaGroup]:
    return [group for group in groups if group.id in kept_group_ids]


# Competencies and their lineage


def _tags_of(course_level_groups: list[CompetencyCriteriaGroup]) -> list[Tag]:
    """Return the competencies the course-level groups are about, once each, by id."""
    tags = {group.tag_id: group.tag for group in course_level_groups}
    return sorted(tags.values(), key=lambda tag: tag.id)


def _find_ancestor_tags_by_id(tags: list[Tag]) -> dict[int, Tag]:
    """
    Find the ancestors of ``tags``, one query per level of depth, as a map from id to tag.

    Ancestors are looked up by id, so a tag renamed while the read runs cannot break the lookup.
    Archived ancestors are included defensively, so a lineage never has a gap should a live tag sit
    under an archived one.
    """
    ancestor_tags_by_id: dict[int, Tag] = {}
    parent_ids = _parent_ids_of(tags)
    while parent_ids:
        parents = Tag.objects.in_bulk(parent_ids)
        ancestor_tags_by_id.update(parents)
        # Skipping ids already fetched keeps a corrupt parent cycle from looping forever.
        parent_ids = _parent_ids_of(parents.values()) - ancestor_tags_by_id.keys()
    return ancestor_tags_by_id


def _parent_ids_of(tags: Iterable[Tag]) -> set[int]:
    return {tag.parent_id for tag in tags if tag.parent_id is not None}


def _taxonomy_of(tag: Tag) -> Taxonomy:
    # The course-level query only returns tags of a taxonomy, and loads it; this narrows the type for mypy.
    assert tag.taxonomy is not None
    return tag.taxonomy


def _lineage_of(tag: Tag, ancestor_tags_by_id: dict[int, Tag]) -> tuple[TagLineageEntry, ...]:
    """Return the path from the top of ``tag``'s taxonomy down to ``tag``."""
    path_up = [tag]
    while path_up[-1].parent_id is not None:
        parent = ancestor_tags_by_id[path_up[-1].parent_id]
        if parent in path_up:  # A corrupt parent cycle: stop rather than loop forever.
            break
        path_up.append(parent)
    return tuple(TagLineageEntry(id=step.id, value=step.value) for step in reversed(path_up))


# Assembly


def _taxonomy_data(tags: list[Tag]) -> tuple[CompetencyTaxonomyData, ...]:
    taxonomies_by_id = {taxonomy.id: taxonomy for taxonomy in map(_taxonomy_of, tags)}
    return tuple(
        CompetencyTaxonomyData(id=taxonomy.id, name=taxonomy.name)
        for taxonomy in sorted(taxonomies_by_id.values(), key=lambda taxonomy: taxonomy.id)
    )


def _tag_data(tags: list[Tag]) -> tuple[CompetencyTagData, ...]:
    ancestor_tags_by_id = _find_ancestor_tags_by_id(tags)
    return tuple(
        CompetencyTagData(
            id=tag.id,
            taxonomy_id=_taxonomy_of(tag).id,
            value=tag.value,
            external_id=tag.external_id,
            lineage=_lineage_of(tag, ancestor_tags_by_id),
        )
        for tag in tags
    )


def _group_data(group: CompetencyCriteriaGroup) -> CriteriaGroupData:
    return CriteriaGroupData(
        id=group.id,
        parent_id=group.parent_id,
        tag_id=group.tag_id,
        name=group.name,
        ordering=group.ordering,
        logic_operator=group.logic_operator,
    )


def _criterion_data(criterion: CompetencyCriterion) -> CriterionData:
    rule_type, rule_payload = effective_rule(criterion)
    return CriterionData(
        id=criterion.id,
        group_id=criterion.group_id,
        object_id=criterion.object_tag.object_id,
        rule_type=rule_type,
        rule_payload=rule_payload,
    )


def _ordering_then_id(group: CompetencyCriteriaGroup) -> tuple[int, int]:
    return group.ordering, group.id
