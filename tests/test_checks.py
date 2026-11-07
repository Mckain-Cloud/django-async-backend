from django.contrib.auth import get_user_model
from django.core import checks
from django.db import models
from django.db.models.signals import post_save
from django.test import SimpleTestCase, override_settings
from test_app.models import TestModel

from django_async_backend.checks import (
    check_model_overrides,
    check_sync_receivers,
)


class OverrideProbeModel(models.Model):
    """Overrides save() and clean() without async twins."""

    name = models.CharField(max_length=10)

    class Meta:
        app_label = "test_app"
        managed = False

    def save(self, *args, **kwargs):
        return super().save(*args, **kwargs)

    def clean(self):
        return super().clean()


class PortedOverrideModel(models.Model):
    """Overrides both halves: nothing to report."""

    name = models.CharField(max_length=10)

    class Meta:
        app_label = "test_app"
        managed = False

    def save(self, *args, **kwargs):
        return super().save(*args, **kwargs)

    async def async_save(self, *args, **kwargs):
        return await super().async_save(*args, **kwargs)


def _for(model, result):
    return [e for e in result if e.obj is model]


class ModelOverrideCheckTests(SimpleTestCase):
    def test_reports_sync_override_without_async_twin(self):
        result = check_model_overrides()
        mine = _for(OverrideProbeModel, result)
        self.assertEqual(
            sorted(e.msg for e in mine),
            [
                "test_app.OverrideProbeModel overrides clean() but not aclean().",
                "test_app.OverrideProbeModel overrides save() but not async_save().",
            ],
        )
        self.assertTrue(all(isinstance(e, checks.Warning) for e in mine))
        self.assertTrue(all(e.id == "async_backend.W001" for e in mine))

    def test_ported_override_and_plain_models_are_quiet(self):
        result = check_model_overrides()
        self.assertEqual(_for(PortedOverrideModel, result), [])
        self.assertEqual(_for(TestModel, result), [])
        # AbstractBaseUser.save() is mirrored by the backend's async_save().
        self.assertEqual(_for(get_user_model(), result), [])

    def test_level_setting(self):
        with override_settings(ASYNC_BACKEND_OVERRIDE_CHECK="error"):
            result = _for(OverrideProbeModel, check_model_overrides())
            self.assertTrue(result)
            self.assertTrue(all(isinstance(e, checks.Error) for e in result))
        with override_settings(ASYNC_BACKEND_OVERRIDE_CHECK="off"):
            self.assertEqual(check_model_overrides(), [])


def sync_probe_receiver(sender, **kwargs):
    pass


class SyncReceiverCheckTests(SimpleTestCase):
    def test_lists_sync_receivers_on_request_time_signals(self):
        post_save.connect(sync_probe_receiver, sender=TestModel, dispatch_uid="probe")
        try:
            result = check_sync_receivers()
        finally:
            post_save.disconnect(dispatch_uid="probe", sender=TestModel)
        matches = [e for e in result if "post_save" in e.msg]
        self.assertEqual(len(matches), 1)
        self.assertIn("tests.test_checks.sync_probe_receiver", matches[0].msg)
        self.assertEqual(matches[0].id, "async_backend.W002")

    def test_async_receivers_are_not_listed(self):
        async def areceiver(sender, **kwargs):
            pass

        post_save.connect(areceiver, sender=TestModel, dispatch_uid="aprobe")
        try:
            result = check_sync_receivers()
        finally:
            post_save.disconnect(dispatch_uid="aprobe", sender=TestModel)
        self.assertFalse(any("areceiver" in e.msg for e in result))
