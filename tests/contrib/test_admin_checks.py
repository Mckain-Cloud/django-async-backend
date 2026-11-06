"""The admin system checks must pass once the async site is registered.

``manage.py test`` runs checks before importing the URLconf *or* after,
depending on the iteration order of the check registry (a set), so the suite
only caught a failing admin check by luck. Run them explicitly, with the
site imported, so the result no longer depends on hash seeds."""

from django.core import checks
from django.test import SimpleTestCase


class AsyncAdminChecksTests(SimpleTestCase):
    def test_registered_async_site_passes_admin_checks(self):
        import admin_urls  # noqa: F401  (registers the site and its admins)

        errors = [
            e
            for e in checks.run_checks()
            if e.id.startswith("admin.") and e.level >= checks.ERROR
        ]
        self.assertEqual(errors, [])
