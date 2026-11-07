"""The per-model accessors: aget_next_by_<field>(), aget_next_in_order(),
aget_<model>_order() / aset_<model>_order()."""

from datetime import datetime

from django.contrib.auth.models import User
from test_app.models import (
    DatesModel,
    OrderItemModel,
    OrderParentModel,
    UpdateOrCreateModel,
)

from django_async_backend.test import AsyncioTestCase
from tests.contrib.test_admin import no_sync_queries


class NextPreviousByFieldTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.rows = []
        for day in (1, 2, 3):
            row = await UpdateOrCreateModel.async_objects.acreate(
                name="r%d" % day, value=day
            )
            # auto_now set it on save; pin distinct values behind its back.
            await UpdateOrCreateModel.async_objects.filter(pk=row.pk).aupdate(
                updated_at=datetime(2020, 1, day, 12, 0)
            )
            await row.async_refresh_from_db()
            self.rows.append(row)

    async def test_accessors_mirror_djangos(self):
        self.assertTrue(hasattr(UpdateOrCreateModel, "get_next_by_updated_at"))
        self.assertTrue(
            hasattr(UpdateOrCreateModel, "aget_next_by_updated_at")
        )
        self.assertTrue(
            hasattr(UpdateOrCreateModel, "aget_previous_by_updated_at")
        )
        # Nullable date fields get no accessor, in either form.
        self.assertFalse(hasattr(DatesModel, "get_next_by_date"))
        self.assertFalse(hasattr(DatesModel, "aget_next_by_date"))
        # Third-party models are covered by the startup pass.
        self.assertTrue(hasattr(User, "aget_next_by_date_joined"))

    async def test_next_and_previous(self):
        r1, r2, r3 = self.rows
        with no_sync_queries():
            self.assertEqual(await r1.aget_next_by_updated_at(), r2)
            self.assertEqual(await r2.aget_next_by_updated_at(), r3)
            self.assertEqual(await r3.aget_previous_by_updated_at(), r2)
            self.assertEqual(await r2.aget_previous_by_updated_at(), r1)

    async def test_ends_raise_does_not_exist(self):
        r1, _, r3 = self.rows
        with no_sync_queries():
            with self.assertRaises(UpdateOrCreateModel.DoesNotExist):
                await r3.aget_next_by_updated_at()
            with self.assertRaises(UpdateOrCreateModel.DoesNotExist):
                await r1.aget_previous_by_updated_at()

    async def test_extra_filters(self):
        r1, _, r3 = self.rows
        with no_sync_queries():
            self.assertEqual(await r1.aget_next_by_updated_at(value__gt=2), r3)

    async def test_ties_are_broken_by_pk(self):
        r1, r2, r3 = self.rows
        await UpdateOrCreateModel.async_objects.aupdate(
            updated_at=datetime(2020, 1, 1, 12, 0)
        )
        for row in self.rows:
            await row.async_refresh_from_db()
        with no_sync_queries():
            self.assertEqual(await r1.aget_next_by_updated_at(), r2)
            self.assertEqual(await r3.aget_previous_by_updated_at(), r2)

    async def test_unsaved_instance(self):
        with self.assertRaises(ValueError) as cm:
            await UpdateOrCreateModel(name="x").aget_next_by_updated_at()
        self.assertEqual(
            str(cm.exception),
            "get_next/get_previous cannot be used on unsaved objects.",
        )


class OrderWithRespectToTests(AsyncioTestCase):
    async def asyncSetUp(self):
        self.parent = await OrderParentModel.async_objects.acreate(name="p")
        self.items = [
            await OrderItemModel.async_objects.acreate(parent=self.parent)
            for _ in range(3)
        ]

    async def test_accessors_mirror_djangos(self):
        self.assertTrue(hasattr(OrderItemModel, "get_next_in_order"))
        self.assertTrue(hasattr(OrderItemModel, "aget_next_in_order"))
        self.assertTrue(hasattr(OrderItemModel, "aget_previous_in_order"))
        self.assertTrue(hasattr(OrderParentModel, "get_orderitemmodel_order"))
        self.assertTrue(hasattr(OrderParentModel, "aget_orderitemmodel_order"))
        self.assertTrue(hasattr(OrderParentModel, "aset_orderitemmodel_order"))

    async def test_get_and_set_order(self):
        i1, i2, i3 = self.items
        with no_sync_queries():
            self.assertEqual(
                await self.parent.aget_orderitemmodel_order(),
                [i1.pk, i2.pk, i3.pk],
            )
            await self.parent.aset_orderitemmodel_order([i3.pk, i1.pk, i2.pk])
            self.assertEqual(
                await self.parent.aget_orderitemmodel_order(),
                [i3.pk, i1.pk, i2.pk],
            )

    async def test_next_and_previous_in_order(self):
        i1, i2, i3 = self.items
        with no_sync_queries():
            self.assertEqual(await i1.aget_next_in_order(), i2)
            self.assertEqual(await i2.aget_next_in_order(), i3)
            self.assertEqual(await i3.aget_previous_in_order(), i2)
            with self.assertRaises(OrderItemModel.DoesNotExist):
                await i3.aget_next_in_order()
            with self.assertRaises(OrderItemModel.DoesNotExist):
                await i1.aget_previous_in_order()

    async def test_follows_a_reordering(self):
        i1, i2, i3 = self.items
        with no_sync_queries():
            await self.parent.aset_orderitemmodel_order([i3.pk, i1.pk, i2.pk])
            # Fresh instances: the result is cached on the instance, as in
            # Django.
            first = await OrderItemModel.async_objects.aget(pk=i3.pk)
            self.assertEqual(await first.aget_next_in_order(), i1)
            last = await OrderItemModel.async_objects.aget(pk=i2.pk)
            self.assertEqual(await last.aget_previous_in_order(), i1)
