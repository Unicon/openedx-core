"""
CBE API v1 URLs for learners: the read-only routes an LMS serves.
"""

from django.urls import path

from . import views

urlpatterns = [
    # The path converter lets a deprecated slash key (Org/Course/Run) reach the view, and a malformed
    # value containing a slash get a 400 from it instead of a 404 from the router.
    path(
        "courses/<path:course_key>/competency-criteria/",
        views.CourseCompetencyCriteriaView.as_view(),
        name="course-competency-criteria",
    ),
]
