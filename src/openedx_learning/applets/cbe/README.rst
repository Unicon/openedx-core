Competency-Based Education Applet
=================================

The ``cbe`` applet models learner mastery of competencies. A competency is a tag; a
competency taxonomy is a taxonomy whose tags are competencies rather than ordinary labels.

``CompetencyTaxonomy`` marks a taxonomy as one of those. It is a Django multi-table
inheritance subclass of ``openedx_tagging``'s ``Taxonomy``, so the two share a primary key:
a competency taxonomy is the same taxonomy, not a copy of one. Creating one writes both
rows in a single transaction, and deleting either row removes both.

Ask whether a taxonomy is competency-enabled through ``openedx_learning.api``, not by
reaching for ``taxonomy.competencytaxonomy``. ``is_competency_taxonomy()`` answers for one
taxonomy; put a queryset through ``select_competency_taxonomies()`` first when checking a
list, so the whole list costs one query instead of one per row. Keeping the check here is
what lets ``openedx_tagging`` stay a generic tagging library that never learns CBE exists.

The criteria, rule profile, and learner status tables are designed in
``docs/openedx_learning/decisions/``. ``openedx_tagging`` ADR 0013 covers how
``openedx-platform`` calls ``is_competency_taxonomy()`` to report a taxonomy's type to
Studio.

Learner routes
--------------

``openedx_learning.learner_urls`` holds the route listing a course run's competency criteria,
``cbe/v1/courses/<course_key>/competency-criteria/`` below its mount point. Mount it in the LMS, apart
from ``openedx_learning.urls``, the CMS's authoring routes. An LMS mounting only the learner URLs answers
every authoring route with 404.

Access is the permission ``openedx_learning.view_course_competency_criteria``, available as
``openedx_learning.rules.VIEW_COURSE_COMPETENCY_CRITERIA`` and checked with the course key. The default
admits any signed-in user, so an LMS should rebind it with ``rules.set_perm`` to a rule that checks, for
example, enrollment. openedx-core registers its default only if no rule is registered under that name
yet, so the host's rule wins whether it is registered before or after openedx-core loads.
