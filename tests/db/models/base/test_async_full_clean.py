import datetime

from django.core.exceptions import ValidationError
from django.db import NotSupportedError
from django.db.models import (
    CheckConstraint,
    Q,
)
from django.db.models.constraints import BaseConstraint
from test_app.models import (
    TestModel,
    TotalOrderingModel,
)

from django_async_backend.db.models.constraints import (
    aq_check,
    avalidate_constraint,
)
from django_async_backend.db.transaction import async_atomic
from django_async_backend.test import AsyncioTestCase


class AsyncFullCleanTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.existing = await TestModel.async_objects.acreate(
            name="taken", value=1
        )

    async def test_valid_instance_passes(self):
        await TestModel(name="free", value=1).async_full_clean()

    async def test_unique_field_violation(self):
        with self.assertRaises(ValidationError) as cm:
            await TestModel(name="taken", value=1).async_full_clean()
        self.assertEqual(list(cm.exception.message_dict), ["name"])

    async def test_editing_excludes_itself(self):
        self.existing.value = 2
        await self.existing.async_full_clean()

    async def test_exclude_skips_the_check(self):
        await TestModel(name="taken", value=1).async_full_clean(
            exclude={"name"}
        )

    async def test_validate_unique_can_be_disabled(self):
        await TestModel(name="taken", value=1).async_full_clean(
            validate_unique=False
        )

    async def test_field_errors_are_collected_with_unique_errors(self):
        with self.assertRaises(ValidationError) as cm:
            await TestModel(name="", value=None).async_full_clean()
        self.assertEqual(sorted(cm.exception.message_dict), ["name", "value"])

    async def test_sees_rows_from_the_surrounding_transaction(self):
        async with async_atomic():
            await TestModel.async_objects.acreate(name="in-tx", value=1)
            with self.assertRaises(ValidationError):
                await TestModel(name="in-tx", value=1).async_full_clean()

    async def test_unique_together(self):
        await TotalOrderingModel.async_objects.acreate(
            headline="h", slug="s", barcode="b1"
        )
        with self.assertRaises(ValidationError) as cm:
            await TotalOrderingModel(
                headline="h", slug="s", barcode="b2"
            ).async_full_clean()
        self.assertIn("__all__", cm.exception.message_dict)

    async def test_unique_constraints_without_condition(self):
        await TotalOrderingModel.async_objects.acreate(
            headline="h1",
            rank=1,
            pub_date=datetime.date(2026, 1, 1),
            barcode="b1",
        )
        obj = TotalOrderingModel(
            headline="h2",
            rank=1,
            pub_date=datetime.date(2026, 1, 1),
            barcode="b2",
        )
        with self.assertRaises(ValidationError) as cm:
            await obj.async_validate_constraints()
        self.assertIn("__all__", cm.exception.message_dict)

    async def test_conditional_unique_constraint_uses_aq_check(self):
        await TotalOrderingModel.async_objects.acreate(
            headline="h1", rank=5, barcode="b1"
        )
        # rank=5 clashes under Q(rank__gt=0)...
        with self.assertRaises(ValidationError):
            await TotalOrderingModel(
                headline="h2", rank=5, barcode="b2"
            ).async_validate_constraints()
        # ...but the condition excludes rank <= 0, so no clash there.
        await TotalOrderingModel.async_objects.acreate(
            headline="h3", rank=-1, barcode="b3"
        )
        await TotalOrderingModel(
            headline="h4", rank=-1, barcode="b4"
        ).async_validate_constraints(exclude={"rank"})

    async def test_check_constraint(self):
        constraint = CheckConstraint(condition=Q(rank__gt=0), name="positive")
        await avalidate_constraint(
            constraint, TotalOrderingModel, TotalOrderingModel(rank=3)
        )
        with self.assertRaises(ValidationError):
            await avalidate_constraint(
                constraint, TotalOrderingModel, TotalOrderingModel(rank=-3)
            )

    async def test_aq_check(self):
        self.assertTrue(await aq_check(Q(a__gt=1), {"a": 2}))
        self.assertFalse(await aq_check(Q(a__gt=1), {"a": 0}))

    async def test_unknown_constraint_type_is_rejected(self):
        class Weird(BaseConstraint):
            def validate(self, *args, **kwargs):  # pragma: no cover
                pass

        with self.assertRaises(NotSupportedError):
            await avalidate_constraint(
                Weird(name="weird"), TotalOrderingModel, TotalOrderingModel()
            )
