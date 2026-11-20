"""
Learner-facing Learning API URLs, for the routes an LMS serves.

A host that serves learners but not course authoring mounts this module and not ``urls``, so the
authoring routes answer 404 there. Like ``urls``, it maps each applet onto its own URL segment and
declares no ``app_name`` itself.
"""

from django.urls import include, path

from .applets.cbe.rest_api import learner_urls as cbe_learner_urls

urlpatterns = [path("cbe/", include(cbe_learner_urls))]
