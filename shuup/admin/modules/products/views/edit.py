# -*- coding: utf-8 -*-
# This file is part of Shuup.
#
# Copyright (c) 2012-2016, Shoop Ltd. All rights reserved.
#
# This source code is licensed under the AGPLv3 license found in the
# LICENSE file in the root directory of this source tree.
from __future__ import unicode_literals

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.db.transaction import atomic
from django.utils.translation import ugettext as _
from django.utils.translation import get_language

from shuup.admin.form_part import (
    FormPart, FormPartsViewMixin, SaveFormPartsMixin, TemplatedFormDef
)
from shuup.admin.modules.products.forms import (
    ProductAttributesForm, ProductBaseForm, ProductImageMediaFormSet,
    ProductMediaFormSet, ShopProductForm
)
from shuup.admin.utils.views import CreateOrUpdateView
from shuup.apps.provides import get_provide_objects
from shuup.core.models import (
    Product, ProductType, SalesUnit, Shop, ShopProduct, ShopStatus, Supplier,
    TaxClass
)

from .toolbars import EditProductToolbar


class ProductBaseFormPart(FormPart):
    priority = -1000  # Show this first, no matter what

    def get_form_defs(self):
        yield TemplatedFormDef(
            "base",
            ProductBaseForm,
            template_name="shuup/admin/products/_edit_base_form.jinja",
            required=True,
            kwargs={
                "instance": self.object,
                "languages": settings.LANGUAGES,
                "initial": self.get_initial()
            }
        )

        yield TemplatedFormDef(
            "base_extra",
            forms.Form,
            template_name="shuup/admin/products/_edit_extra_base_form.jinja",
            required=False
        )

    def form_valid(self, form):
        self.object = form["base"].save()
        return self.object

    def get_sku(self):
        sku = self.request.GET.get("sku", "")
        if not sku:
            last_id = Product.objects.values_list('id', flat=True).first()
            sku = last_id + 1 if last_id else 1
        return sku

    def get_initial(self):
        if not self.object.pk:
            # Sane defaults...
            name_field = "name__%s" % get_language()
            return {
                name_field: self.request.GET.get("name", ""),
                "sku": self.get_sku(),
                "type": ProductType.objects.first(),
                "tax_class": TaxClass.objects.first(),
                "sales_unit": SalesUnit.objects.first()
            }


class ShopProductFormPart(FormPart):
    priority = -900

    def __init__(self, request, object=None):
        super(ShopProductFormPart, self).__init__(request, object)
        self.shops = Shop.objects.filter(status=ShopStatus.ENABLED)

    def get_shop_instance(self, shop):
        try:
            shop_product = self.object.get_shop_instance(shop)
        except ShopProduct.DoesNotExist:
            shop_product = ShopProduct(shop=shop, product=self.object)
        return shop_product

    def get_form_defs(self):
        for shop in self.shops:
            shop_product = self.get_shop_instance(shop)
            yield TemplatedFormDef(
                "shop%d" % shop.pk,
                ShopProductForm,
                template_name="shuup/admin/products/_edit_shop_form.jinja",
                required=True,
                kwargs={"instance": shop_product, "initial": self.get_initial()}
            )

            # the hidden extra form template that uses ShopProductForm
            yield TemplatedFormDef(
                "shop%d_extra" % shop.pk,
                forms.Form,
                template_name="shuup/admin/products/_edit_extra_shop_form.jinja",
                required=False
            )

    def form_valid(self, form):
        for shop in self.shops:
            shop_product_form = form["shop%d" % shop.pk]
            if not shop_product_form.changed_data:
                continue
            if not shop_product_form.instance.pk:
                shop_product_form.instance.product = self.object

            original_quantity = shop_product_form.instance.minimum_purchase_quantity
            rounded_quantity = self.object.sales_unit.round(original_quantity)
            if original_quantity != rounded_quantity:
                messages.info(self.request, _("Minimum Purchase Quantity has been rounded to match Sales Unit."))

            shop_product_form.instance.minimum_purchase_quantity = rounded_quantity

            inst = shop_product_form.save()
            messages.success(self.request, _("Changes to shop instance for %s saved") % inst.shop)

    def get_initial(self):
        if not self.object.pk:
            return {
                "suppliers": [Supplier.objects.first()]
            }


class ProductAttributeFormPart(FormPart):
    priority = -800

    def get_form_defs(self):
        if not self.object.get_available_attribute_queryset():
            return
        yield TemplatedFormDef(
            "attributes",
            ProductAttributesForm,
            template_name="shuup/admin/products/_edit_attribute_form.jinja",
            required=False,
            kwargs={"product": self.object, "languages": settings.LANGUAGES}
        )

    def form_valid(self, form):
        if "attributes" in form.forms:
            form.forms["attributes"].save()


class BaseProductMediaFormPart(FormPart):
    def get_form_defs(self):
        yield TemplatedFormDef(
            self.name,
            self.formset,
            template_name="shuup/admin/products/_edit_media_form.jinja",
            required=False,
            kwargs={"product": self.object, "languages": settings.LANGUAGES}
        )

    def form_valid(self, form):
        if self.name in form.forms:
            frm = form.forms[self.name]
            frm.save()


class ProductMediaFormPart(BaseProductMediaFormPart):
    name = "media"
    priority = -700
    formset = ProductMediaFormSet


class ProductImageMediaFormPart(BaseProductMediaFormPart):
    name = "images"
    priority = -600
    formset = ProductImageMediaFormSet


class ProductEditView(SaveFormPartsMixin, FormPartsViewMixin, CreateOrUpdateView):
    model = Product
    context_object_name = "product"
    template_name = "shuup/admin/products/edit.jinja"
    base_form_part_classes = [
        ProductBaseFormPart,
        ShopProductFormPart,
        ProductAttributeFormPart,
        ProductImageMediaFormPart,
        ProductMediaFormPart
    ]
    form_part_class_provide_key = "admin_product_form_part"

    @atomic
    def form_valid(self, form):
        return self.save_form_parts(form)

    def get_toolbar(self):
        return EditProductToolbar(view=self)

    def get_context_data(self, **kwargs):
        context = super(ProductEditView, self).get_context_data(**kwargs)
        orderability_errors = []

        if self.object.pk:
            for shop in Shop.objects.all():
                try:
                    shop_product = self.object.get_shop_instance(shop)
                    orderability_errors.extend(
                        ["%s: %s" % (shop.name, msg.message)
                         for msg in shop_product.get_orderability_errors(
                            supplier=None,
                            quantity=shop_product.minimum_purchase_quantity,
                            customer=None)])
                except ObjectDoesNotExist:
                    orderability_errors.extend(["%s: %s" % (shop.name, _("Product is not available."))])
        context["orderability_errors"] = orderability_errors
        context["product_sections"] = []
        product_sections_provides = sorted(get_provide_objects("admin_product_section"), key=lambda x: x.order)
        for admin_product_section in product_sections_provides:
            if admin_product_section.visible_for_object(self.object):
                context["product_sections"].append(admin_product_section)
                context[admin_product_section.identifier] = admin_product_section.get_context_data(self.object)

        return context
