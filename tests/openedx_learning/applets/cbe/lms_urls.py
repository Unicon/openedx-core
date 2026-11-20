"""A URLconf like an LMS's: only the learner routes, under the "api/" prefix the CMS mounts authoring at."""
from django.urls import include, path

urlpatterns = [path("api/", include("openedx_learning.learner_urls"))]
