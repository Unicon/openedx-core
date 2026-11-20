"""
CBE API URLs for learners, the routes an LMS mounts.
"""

from django.urls import include, path

from .v1 import learner_urls as v1_learner_urls

# Its own namespace, so the learner routes reverse as "cbe_learner:<name>" and do not clash with the
# authoring routes under "cbe:" when a project mounts both.
app_name = "cbe_learner"

urlpatterns = [path("v1/", include(v1_learner_urls))]
