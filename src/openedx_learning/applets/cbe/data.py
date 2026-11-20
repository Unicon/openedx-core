"""
Value objects passed across the CBE applet's public API boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True, kw_only=True, slots=True)
class GradedObjectScore:
    """
    One learner's final, override-adjusted fraction earned on one graded object.

    ``fraction`` is the caller's final answer for how much of ``object_id`` this learner earned;
    this library cannot detect whether an override was already folded into it. Leave an object
    worth zero possible points out of the list entirely rather than passing it with a
    ``Decimal("0")`` fraction, since it has no meaningful fraction to evaluate. ``Decimal("0")``
    itself is a valid, meaningful input: it means the learner attempted the object and earned
    nothing on it.

    ``fraction`` must be built from a string or from ``Decimal`` arithmetic, never from a
    ``float``: ``Decimal(0.7)`` is a ``Decimal`` instance, so it passes this class's own type
    check, but its value is ``0.69999999999999995559...``, not ``0.7``.
    """

    object_id: str
    fraction: Decimal

    def __post_init__(self) -> None:
        """Enforce this class's type and range contract once, at construction time."""
        if not isinstance(self.fraction, Decimal):
            raise TypeError(f"fraction must be a Decimal, not {type(self.fraction)!r}")
        # is_finite() must run first: 0 <= Decimal("NaN") raises decimal.InvalidOperation, not
        # ValueError, so checking finiteness first is what makes every invalid input, including
        # NaN and Infinity, actually raise the documented ValueError.
        if not self.fraction.is_finite() or not 0 <= self.fraction <= 1:
            raise ValueError(f"fraction must be finite and between 0 and 1 inclusive, not {self.fraction!r}")


@dataclass(frozen=True, kw_only=True, slots=True)
class CriterionDeletionResult:
    """
    The outcome of removing a CompetencyCriterion: whether its row was hard-deleted or archived.

    UNSTABLE: the criteria-authoring family is incomplete, so this shape may change without a
    deprecation cycle.
    """

    id: int
    archived: bool


@dataclass(frozen=True, kw_only=True, slots=True)
class GroupDeletionResult:
    """
    The outcome of removing a CompetencyCriteriaGroup subtree: hard-deleted or archived, with counts.

    The counts cover the target subtree only: ``cascaded_group_count`` counts the target's descendant
    groups, not the target itself and not the ancestors the removal cascaded to, and
    ``cascaded_criteria_count`` is the number of criteria in the subtree.

    UNSTABLE: the criteria-authoring family is incomplete, so this shape may change without a
    deprecation cycle.
    """

    id: int
    archived: bool
    cascaded_group_count: int
    cascaded_criteria_count: int


@dataclass(frozen=True, kw_only=True, slots=True)
class TagLineageEntry:
    """
    One tag on the path from the top of a taxonomy down to a competency.

    UNSTABLE: this shape may change without a deprecation cycle.
    """

    id: int
    value: str


@dataclass(frozen=True, kw_only=True, slots=True)
class CompetencyTaxonomyData:
    """
    A competency taxonomy that holds at least one competency with criteria in the course run.

    UNSTABLE: this shape may change without a deprecation cycle.
    """

    id: int
    name: str


@dataclass(frozen=True, kw_only=True, slots=True)
class CompetencyTagData:
    """
    A competency: a tag of a competency taxonomy.

    ``lineage`` runs from the top of the taxonomy down to the competency itself, which is its last entry.

    UNSTABLE: this shape may change without a deprecation cycle.
    """

    id: int
    taxonomy_id: int
    value: str
    external_id: str
    lineage: tuple[TagLineageEntry, ...]


@dataclass(frozen=True, kw_only=True, slots=True)
class CriteriaGroupData:
    """
    A criteria group. A root group has no ``parent_id``; every group names its competency through ``tag_id``.

    ``logic_operator`` is ``"AND"``, ``"OR"`` or ``None``. ``None`` means no operator was stored: the root and
    course-level groups the authoring API creates carry ``None`` however many children they have, and a
    reader treats ``None`` as ``OR``.

    UNSTABLE: this shape may change without a deprecation cycle.
    """

    id: int
    parent_id: int | None
    tag_id: int
    name: str
    ordering: int
    logic_operator: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class CriterionData:
    """
    A criterion: the rule a learner must meet on one object, such as a graded subsection, to count towards a group.

    ``rule_type`` and ``rule_payload`` are the effective rule, whether it comes from the criterion's rule
    profile or from its own override, with the payload exactly as stored. A Grade payload's ``value`` is a
    fraction from 0.0 to 1.0 (see ``GradeRulePayload``), even though its ``scale`` reads ``"percent"``.

    UNSTABLE: this shape may change without a deprecation cycle.
    """

    id: int
    group_id: int
    object_id: str
    rule_type: str
    rule_payload: Any


@dataclass(frozen=True, kw_only=True, slots=True)
class CourseCompetencyCriteria:
    """
    Everything the learner-facing criteria read returns for one course run.

    Each collection is a tuple. Every reference field is named after the collection it points into
    (``taxonomy_id`` into ``taxonomies``, ``tag_id`` into ``tags``, ``parent_id`` and ``group_id`` into
    ``groups``), and every id it holds belongs to an entry there, so the taxonomy, competency, group and
    criterion nesting can be rebuilt from this object alone. Authoring-side columns, such as the rule
    profile id, the override columns and the object tag id, are left out on purpose.

    The learner endpoint returns this object as its JSON body, so this class and the classes it holds are
    the description of the wire format.

    UNSTABLE: this shape may change without a deprecation cycle.
    """

    taxonomies: tuple[CompetencyTaxonomyData, ...]
    tags: tuple[CompetencyTagData, ...]
    groups: tuple[CriteriaGroupData, ...]
    criteria: tuple[CriterionData, ...]
