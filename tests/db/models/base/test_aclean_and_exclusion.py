from django.core.exceptions import ValidationError
from test_app.models import (
    TestModel,
    TotalOrderingModel,
)

from django_async_backend.db.models.constraints import avalidate_constraint
from django_async_backend.test import AsyncioTestCase


class AsyncCleanHookTests(AsyncioTestCase):
    async def test_aclean_is_awaited_by_async_full_clean(self):
        async def aclean(self):
            if await TestModel.async_objects.filter(
                value=self.value
            ).aexists():
                raise ValidationError({"value": "value taken (aclean)"})

        TestModel.aclean = aclean
        try:
            await TestModel.async_objects.acreate(name="x", value=7)
            with self.assertRaises(ValidationError) as cm:
                await TestModel(name="y", value=7).async_full_clean()
            self.assertIn("value taken (aclean)", str(cm.exception))
            await TestModel(name="z", value=8).async_full_clean()
        finally:
            del TestModel.aclean


class ExclusionConstraintTests(AsyncioTestCase):
    async def test_exclusion_constraint_is_validated(self):
        from django.contrib.postgres.constraints import ExclusionConstraint
        from django.contrib.postgres.fields import RangeOperators

        constraint = ExclusionConstraint(
            name="no_same_rank",
            expressions=[("rank", RangeOperators.EQUAL)],
        )
        await TotalOrderingModel.async_objects.acreate(
            headline="h1", rank=3, barcode="b1"
        )
        await avalidate_constraint(
            constraint, TotalOrderingModel, TotalOrderingModel(rank=4)
        )
        with self.assertRaises(ValidationError):
            await avalidate_constraint(
                constraint, TotalOrderingModel, TotalOrderingModel(rank=3)
            )
