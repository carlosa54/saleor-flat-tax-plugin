import json
import numbers
from typing import TYPE_CHECKING, Any, Iterable, List, Optional, Union
from decimal import Decimal
from django.core.exceptions import ValidationError
from django_countries.fields import Country
from prices import Money, TaxedMoney, TaxedMoneyRange

from saleor.checkout import base_calculations
from saleor.core.prices import quantize_price
from saleor.core.taxes import TaxType, zero_money, zero_taxed_money, TaxLineData, TaxData
from saleor.discount import VoucherType
from saleor.plugins.error_codes import PluginErrorCode
from saleor.order.interface import OrderTaxedPricesData
from saleor.order.utils import (
    get_total_order_discount_excluding_shipping,
    get_voucher_discount_assigned_to_order,
)
from saleor.product.models import ProductType
from saleor.plugins.base_plugin import BasePlugin, ConfigurationTypeField
from saleor.plugins.manager import get_plugins_manager
from . import (
    DEFAULT_TAX_RATE_NAME,
    apply_checkout_discount_on_checkout_line,
    apply_tax_to_price,
    get_taxed_shipping_price,
    get_tax_for_rate,
    META_CODE_KEY,
    META_DESCRIPTION_KEY,
)

if TYPE_CHECKING:
    # flake8: noqa
    from saleor.account.models import Address
    from saleor.channel.models import Channel
    from saleor.checkout.fetch import CheckoutInfo, CheckoutLineInfo
    from saleor.discount import DiscountInfo
    from saleor.order.models import Order, OrderLine
    from saleor.product.models import (
        Product,
        ProductVariant,
    )
    from saleor.plugins.models import PluginConfiguration


class FlatTaxPlugin(BasePlugin):
    PLUGIN_ID = "taxes.flattax"
    PLUGIN_NAME = "Flat Tax"

    DEFAULT_CONFIGURATION = [
        {"name": "flat_taxes", "value": '{"standard": 10, "custom": 10}'},
    ]

    CONFIG_STRUCTURE = {
        "flat_taxes": {
            "type": ConfigurationTypeField.MULTILINE,
            "help_test": (
                "Enter a valid JSON object with the tax name as key and tax amount as value.\n"
                "You can follow the default flat_taxes example schema."
            ),
            "label": "Flat Taxes",
        },
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        configuration = {item["name"]: item["value"] for item in self.configuration}
        flat_taxes = configuration.pop("flat_taxes")

        self.flat_taxes = json.loads(flat_taxes)

    @classmethod
    def validate_plugin_configuration(cls, plugin_configuration: "PluginConfiguration"):
        """Validate if provided configuration is correct."""
        configuration = plugin_configuration.configuration
        configuration = {item["name"]: item["value"] for item in configuration}
        flat_taxes = configuration.pop("flat_taxes")

        try:
            flat_taxes = json.loads(flat_taxes)
        except json.JSONDecodeError:
            raise ValidationError({
                "flat_taxes": ValidationError(
                    "flat_taxes must be a valid JSON string",
                    code=PluginErrorCode.INVALID.value
                )
            })

        all_tax_items_valid = all(
            [isinstance(tax_value, numbers.Number) for tax_value in flat_taxes.values()]
        )

        if not all_tax_items_valid:
            raise ValidationError({
                "flat_taxes": ValidationError(
                    "All tax items must be valid",
                    code=PluginErrorCode.INVALID.value
                )
            })

    def _skip_plugin(
            self,
            previous_value: Union[
                TaxedMoney,
                TaxedMoneyRange,
                Decimal,
                OrderTaxedPricesData,
            ]
    ) -> bool:
        if not self.active:
            return True

        # The previous plugin already calculated taxes so we can skip our logic
        if isinstance(previous_value, TaxedMoneyRange):
            start = previous_value.start
            stop = previous_value.stop

            return start.net != start.gross and stop.net != stop.gross

        if isinstance(previous_value, TaxedMoney):
            return previous_value.net != previous_value.gross

        if isinstance(previous_value, OrderTaxedPricesData):
            return (
                previous_value.price_with_discounts.net
                != previous_value.price_with_discounts.gross
            )

        return False

    def _get_taxes(self):
        taxes_from_config = self.flat_taxes
        taxes = {
            tax_name: {
                "value": taxes_from_config[tax_name],
                "tax": get_tax_for_rate(taxes_from_config, tax_name),
            } for tax_name in taxes_from_config
        }

        return taxes

    def get_taxes_for_order(
            self, order: "Order", previous_value
    ) -> Optional["TaxData"]:
        if self._skip_plugin(previous_value):
            return previous_value

        if self.channel != order.channel:
            return previous_value

        lines = self.update_taxes_for_order_lines(order, order.lines.all(), None)
        lines = [
            TaxLineData(
                total_net_amount=order_line.total_price_net_amount,
                total_gross_amount=order_line.total_price_gross_amount,
                tax_rate=self._get_tax_rate(order_line.variant.product, None, Decimal('0')) * 100
            )
            for order_line in lines
        ]

        order_shipping = self.calculate_order_shipping(order, None) or zero_taxed_money(order.currency)
        # Saleor expects the tax_rate as 10 instead of 0.10 hence we multiply by 100
        shipping_tax_rate = self._get_shipping_tax_rate(Decimal('0')) * 100

        return TaxData(
            shipping_price_net_amount=order_shipping.net.amount,
            shipping_price_gross_amount=order_shipping.gross.amount,
            shipping_tax_rate=shipping_tax_rate,
            lines=lines,
        )

    def calculate_checkout_total(
            self,
            checkout_info: "CheckoutInfo",
            lines: List["CheckoutLineInfo"],
            address: Optional["Address"],
            discounts: List["DiscountInfo"],
            previous_value: TaxedMoney,
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        manager = get_plugins_manager()
        return manager.calculate_checkout_subtotal(
            checkout_info, lines, address, discounts
        ) + manager.calculate_checkout_shipping(
            checkout_info, lines, address, discounts
        )

    def calculate_checkout_shipping(
            self,
            checkout_info: "CheckoutInfo",
            lines: List["CheckoutLineInfo"],
            address: Optional["Address"],
            discounts: List["DiscountInfo"],
            previous_value: TaxedMoney,
    ) -> TaxedMoney:
        """Calculate shipping gross for checkout."""
        if self._skip_plugin(previous_value):
            return previous_value

        taxes = self._get_taxes()
        if not checkout_info.delivery_method_info.delivery_method:
            return previous_value
        shipping_price = getattr(
            checkout_info.delivery_method_info.delivery_method, "price", previous_value
        )
        voucher = checkout_info.voucher
        is_shipping_discount = (
            voucher.type == VoucherType.SHIPPING if voucher else False
        )
        if is_shipping_discount:
            shipping_price = max(
                shipping_price - checkout_info.checkout.discount,
                zero_money(shipping_price.currency),
            )

        return get_taxed_shipping_price(shipping_price, taxes)

    def calculate_order_shipping(self, order: "Order", previous_value: Any) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        taxes = self._get_taxes()
        if not order.shipping_method:
            return previous_value
        shipping_price = order.shipping_method.channel_listings.get(
            channel_id=order.channel_id
        ).price

        if (
                order.voucher_id
                and order.voucher.type == VoucherType.SHIPPING  # type: ignore
        ):
            shipping_discount = get_voucher_discount_assigned_to_order(order)
            if shipping_discount:
                shipping_price = Money(
                    max(
                        shipping_price.amount - shipping_discount.amount_value,
                        Decimal("0"),
                    ),
                    shipping_price.currency,
                )

        return get_taxed_shipping_price(shipping_price, taxes)

    def update_taxes_for_order_lines(
            self,
            order: "Order",
            lines: List["OrderLine"],
            previous_value: Any,
    ) -> List["OrderLine"]:
        if self._skip_plugin(previous_value):
            return previous_value

        address = order.shipping_address or order.billing_address
        country = address.country if address else None
        currency = order.currency

        total_discount_amount = get_total_order_discount_excluding_shipping(
            order
        ).amount
        order_total_price = sum(
            [line.base_unit_price.amount * line.quantity for line in lines]
        )
        total_line_discounts = 0
        for line in lines:
            variant = line.variant
            if not variant:
                continue
            product = variant.product  # type: ignore

            line_total_price = line.base_unit_price * line.quantity
            price_with_discounts = line.base_unit_price
            if total_discount_amount:
                if line is lines[-1]:
                    # for the last line applied remaining discount
                    discount_amount = total_discount_amount - total_line_discounts
                else:
                    # calculate discount proportionally to the rate of total line price
                    # to order total price.
                    discount_amount = quantize_price(
                        line_total_price.amount
                        / order_total_price
                        * total_discount_amount,
                        currency,
                    )
                price_with_discounts = max(
                    quantize_price(
                        (line_total_price - Money(discount_amount, currency))
                        / line.quantity,
                        currency,
                    ),
                    zero_money(currency),
                )
                # sum already applied discounts
                total_line_discounts += discount_amount

            self._update_line_prices(line, price_with_discounts, product, country)

        return lines

    def _update_line_prices(
            self,
            line: "OrderLine",
            price_with_discounts: Money,
            product: "Product",
            country: "Country",
    ):
        line.unit_price = self.__apply_taxes_to_product(
            product, price_with_discounts
        )
        line.undiscounted_unit_price = self.__apply_taxes_to_product(
            product, line.undiscounted_base_unit_price
        )
        line.total_price = line.unit_price * line.quantity
        line.undiscounted_total_price = line.undiscounted_unit_price * line.quantity
        line.tax_rate = self._get_tax_rate(product, None, Decimal('0'))

    def calculate_checkout_line_total(
            self,
            checkout_info: "CheckoutInfo",
            lines: List["CheckoutLineInfo"],
            checkout_line_info: "CheckoutLineInfo",
            address: Optional["Address"],
            discounts: Iterable["DiscountInfo"],
            previous_value: TaxedMoney,
    ) -> TaxedMoney:
        unit_taxed_price = self.__calculate_checkout_line_unit_price(
            checkout_info,
            lines,
            checkout_line_info,
            checkout_info.channel,
            discounts,
            address,
            previous_value,
        )
        if unit_taxed_price is None:
            return previous_value

        quantity = checkout_line_info.line.quantity
        return unit_taxed_price * quantity

    def calculate_checkout_line_unit_price(
            self,
            checkout_info: "CheckoutInfo",
            lines: List["CheckoutLineInfo"],
            checkout_line_info: "CheckoutLineInfo",
            address: Optional["Address"],
            discounts: List["DiscountInfo"],
            previous_value: TaxedMoney,
    ) -> TaxedMoney:
        unit_taxed_price = self.__calculate_checkout_line_unit_price(
            checkout_info,
            lines,
            checkout_line_info,
            checkout_info.channel,
            discounts,
            address,
            previous_value,
        )
        return unit_taxed_price if unit_taxed_price is not None else previous_value

    def __calculate_checkout_line_unit_price(
            self,
            checkout_info: "CheckoutInfo",
            lines: List["CheckoutLineInfo"],
            checkout_line_info: "CheckoutLineInfo",
            channel: "Channel",
            discounts: Iterable["DiscountInfo"],
            address: Optional["Address"],
            previous_value: TaxedMoney,
    ):
        if self._skip_plugin(previous_value):
            return

        unit_price = base_calculations.calculate_base_line_unit_price(
            checkout_line_info,
            channel,
            discounts,
        )

        unit_price = apply_checkout_discount_on_checkout_line(
            checkout_info,
            lines,
            checkout_line_info,
            discounts,
            unit_price,
        )

        return self.__apply_taxes_to_product(
            checkout_line_info.product, unit_price
        )

    def get_checkout_line_tax_rate(
            self,
            checkout_info: "CheckoutInfo",
            lines: Iterable["CheckoutLineInfo"],
            checkout_line_info: "CheckoutLineInfo",
            address: Optional["Address"],
            discounts: Iterable["DiscountInfo"],
            previous_value: Decimal,
    ) -> Decimal:
        return self._get_tax_rate(checkout_line_info.product, address, previous_value)

    def get_order_line_tax_rate(
            self,
            order: "Order",
            product: "Product",
            variant: "ProductVariant",
            address: Optional["Address"],
            previous_value: Decimal,
    ) -> Decimal:
        return self._get_tax_rate(product, address, previous_value)

    def _get_tax_rate(
            self, product: "Product", address: Optional["Address"], previous_value: Decimal
    ):
        if self._skip_plugin(previous_value):
            return previous_value
        taxes, tax_rate = self.__get_tax_data_for_product(product)
        if not taxes or not tax_rate:
            return previous_value
        tax = taxes.get(tax_rate) or taxes.get(DEFAULT_TAX_RATE_NAME)
        # tax value is given in percentage so it need be be converted into decimal value
        return Decimal(tax["value"]) / 100

    def get_checkout_shipping_tax_rate(
            self,
            _checkout_info: "CheckoutInfo",
            _lines: Iterable["CheckoutLineInfo"],
            _address: Optional["Address"],
            _discounts: Iterable["DiscountInfo"],
            previous_value: Decimal,
    ):
        return self._get_shipping_tax_rate(previous_value)

    def get_order_shipping_tax_rate(self, order: "Order", previous_value: Decimal):
        return self._get_shipping_tax_rate(previous_value)

    def _get_shipping_tax_rate(
            self, previous_value: Decimal
    ):
        if self._skip_plugin(previous_value):
            return previous_value
        taxes = self._get_taxes()
        tax = taxes.get(DEFAULT_TAX_RATE_NAME)
        # tax value is in percentage so it needs to be converted into decimal value
        return Decimal(tax["value"]) / 100

    def get_tax_rate_type_choices(
            self, previous_value: List["TaxType"]
    ) -> List["TaxType"]:
        if not self.active:
            return previous_value

        rate_types = self.flat_taxes.keys()
        choices = [
            TaxType(code=rate_name, description=rate_name) for rate_name in rate_types
        ]
        # sort choices alphabetically by translations
        return sorted(choices, key=lambda x: x.code)

    def show_taxes_on_storefront(self, previous_value: bool) -> bool:
        if not self.active:
            return previous_value
        return False

    def apply_taxes_to_shipping(
            self, price: Money, shipping_address: "Address", previous_value: TaxedMoney
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value

        taxes = self._get_taxes()
        return get_taxed_shipping_price(price, taxes)

    def apply_taxes_to_product(
            self,
            product: "Product",
            price: Money,
            country: Country,
            previous_value: TaxedMoney,
    ) -> TaxedMoney:
        if self._skip_plugin(previous_value):
            return previous_value
        return self.__apply_taxes_to_product(product, price)

    def __apply_taxes_to_product(
            self, product: "Product", price: Money
    ):
        taxes, tax_rate = self.__get_tax_data_for_product(product)
        return apply_tax_to_price(taxes, tax_rate, price)

    def __get_tax_data_for_product(self, product: "Product"):
        taxes = None
        if product.charge_taxes:
            taxes = self._get_taxes()
        product_tax_rate = self.__get_tax_code_from_object_meta(product).code
        tax_rate = (
                product_tax_rate
                or self.__get_tax_code_from_object_meta(product.product_type).code
        )
        return taxes, tax_rate

    def assign_tax_code_to_object_meta(
            self,
            obj: Union["Product", "ProductType"],
            tax_code: Optional[str],
            previous_value: Any,
    ):
        if not self.active:
            return previous_value

        if tax_code is None and obj.pk:
            obj.delete_value_from_metadata(META_CODE_KEY)
            obj.delete_value_from_metadata(META_DESCRIPTION_KEY)
        elif tax_code is not None:
            tax_item = {
                META_CODE_KEY: tax_code,
                META_DESCRIPTION_KEY: tax_code,
            }
            obj.store_value_in_metadata(items=tax_item)
        return previous_value

    def get_tax_code_from_object_meta(
            self, obj: Union["Product", "ProductType"], previous_value: "TaxType"
    ) -> "TaxType":
        return self.__get_tax_code_from_object_meta(obj)

    def __get_tax_code_from_object_meta(
            self, obj: Union["Product", "ProductType"]
    ) -> "TaxType":
        # Product has None as it determines if we overwrite taxes for the product
        default_tax_code = None
        default_tax_description = None
        if isinstance(obj, ProductType):
            default_tax_code = DEFAULT_TAX_RATE_NAME
            default_tax_description = DEFAULT_TAX_RATE_NAME

        tax_code = obj.get_value_from_metadata(META_CODE_KEY, default_tax_code)
        tax_description = obj.get_value_from_metadata(
            META_DESCRIPTION_KEY, default_tax_description
        )
        return TaxType(
            code=tax_code,
            description=tax_description,
        )
