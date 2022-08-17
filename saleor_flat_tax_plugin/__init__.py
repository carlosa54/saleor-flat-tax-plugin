from decimal import Decimal
from typing import TYPE_CHECKING, Iterable, List
from prices import Money, MoneyRange, TaxedMoney, TaxedMoneyRange

from saleor.core.prices import quantize_price
from saleor.discount import VoucherType

if TYPE_CHECKING:
    from saleor.checkout.fetch import CheckoutInfo, CheckoutLineInfo
    from saleor.discount import DiscountInfo

META_CODE_KEY = "flattax.code"
META_DESCRIPTION_KEY = "flattax.description"
DEFAULT_TAX_RATE_NAME = "standard"


def _convert_to_naive_taxed_money(base, taxes, rate_name):
    """Naively convert Money to TaxedMoney.

    It is meant for consistency with price handling logic across the codebase,
    passthrough other money types.
    """
    if isinstance(base, Money):
        return TaxedMoney(net=base, gross=base)
    if isinstance(base, MoneyRange):
        return TaxedMoneyRange(
            apply_tax_to_price(taxes, rate_name, base.start),
            apply_tax_to_price(taxes, rate_name, base.stop),
        )
    if isinstance(base, (TaxedMoney, TaxedMoneyRange)):
        return base
    raise TypeError("Unknown base for flat_tax: %r" % (base,))


def apply_tax_to_price(taxes, rate_name, base):
    from saleor.core.taxes import include_taxes_in_prices
    if not taxes or not rate_name:
        return _convert_to_naive_taxed_money(base, taxes, rate_name)

    if rate_name in taxes:
        tax_to_apply = taxes[rate_name]["tax"]
    else:
        tax_to_apply = taxes[DEFAULT_TAX_RATE_NAME]["tax"]

    keep_gross = include_taxes_in_prices()
    return tax_to_apply(base, keep_gross=keep_gross)


# Taken from prices.flat_tax but adding precision
def flat_tax(base, tax_rate, *, keep_gross=False, precision=Decimal(".0001")):
    """Apply a flat tax by either increasing gross or decreasing net amount."""
    fraction = Decimal(1) + tax_rate
    if isinstance(base, (MoneyRange, TaxedMoneyRange)):
        return TaxedMoneyRange(
            flat_tax(base.start, tax_rate, keep_gross=keep_gross),
            flat_tax(base.stop, tax_rate, keep_gross=keep_gross))
    if isinstance(base, TaxedMoney):
        if keep_gross:
            new_net = (base.net / fraction).quantize(precision)
            return TaxedMoney(net=new_net, gross=base.gross)
        else:
            new_gross = (base.gross * fraction).quantize(precision)
            return TaxedMoney(net=base.net, gross=new_gross)
    if isinstance(base, Money):
        if keep_gross:
            net = (base / fraction).quantize(precision)
            return TaxedMoney(net=net, gross=base)
        else:
            gross = (base * fraction).quantize(precision)
            return TaxedMoney(net=base, gross=gross)
    raise TypeError('Unknown base for flat_tax: %r' % (base,))


def get_tax_rate_by_name(rate_name, taxes=None):
    """Return value of tax rate for current taxes."""
    if not taxes or not rate_name:
        tax_rate = 0
    elif rate_name in taxes:
        tax_rate = taxes[rate_name]["value"]
    else:
        tax_rate = taxes[DEFAULT_TAX_RATE_NAME]["value"]

    return tax_rate


def get_taxed_shipping_price(shipping_price, taxes):
    """Calculate shipping price based on settings and taxes."""
    from saleor.core.taxes import charge_taxes_on_shipping
    if not charge_taxes_on_shipping():
        taxes = None
    return apply_tax_to_price(taxes, DEFAULT_TAX_RATE_NAME, shipping_price)


def get_tax_for_rate(tax_rates, rate_name=DEFAULT_TAX_RATE_NAME):
    rate = tax_rates.get(rate_name)
    if rate is None:
        return None

    final_tax_rate = Decimal(rate) / 100

    def tax(base, keep_gross=False):
        return flat_tax(base, final_tax_rate, keep_gross=keep_gross)

    return tax


def apply_checkout_discount_on_checkout_line(
    checkout_info: "CheckoutInfo",
    lines: List["CheckoutLineInfo"],
    checkout_line_info: "CheckoutLineInfo",
    discounts: Iterable["DiscountInfo"],
    line_price: Money,
):
    """Calculate the checkout line price with discounts.
    Include the entire order voucher discount.
    The discount amount is calculated for every line proportionally to
    the rate of total line price to checkout total price.
    """
    from saleor.checkout import base_calculations
    from saleor.core.taxes import zero_money

    voucher = checkout_info.voucher
    if (
        not voucher
        or voucher.apply_once_per_order
        or voucher.type in [VoucherType.SHIPPING, VoucherType.SPECIFIC_PRODUCT]
    ):
        return line_price

    line_quantity = checkout_line_info.line.quantity
    total_discount_amount = checkout_info.checkout.discount_amount
    line_total_price = line_price * line_quantity
    currency = checkout_info.checkout.currency

    # if the checkout has a single line, the whole discount amount will be applied
    # to this line
    if len(lines) == 1:
        return max(
            (line_total_price - Money(total_discount_amount, currency)) / line_quantity,
            zero_money(currency),
        )

    # if the checkout has more lines we need to propagate the discount amount
    # proportionally to total prices of items
    lines_total_prices = [
        base_calculations.calculate_base_line_unit_price(
            line_info,
            checkout_info.channel,
            discounts,
        ).amount
        * line_info.line.quantity
        for line_info in lines
        if line_info.line.id != checkout_line_info.line.id
    ]

    total_price = sum(lines_total_prices) + line_total_price.amount

    last_element = lines[-1].line.id == checkout_line_info.line.id
    if last_element:
        discount_amount = _calculate_discount_for_last_element(
            lines_total_prices, total_price, total_discount_amount, currency
        )
    else:
        discount_amount = quantize_price(
            line_total_price.amount / total_price * total_discount_amount, currency
        )
    return max(
        quantize_price(
            (line_total_price - Money(discount_amount, currency)) / line_quantity,
            currency,
        ),
        zero_money(currency),
    )


def _calculate_discount_for_last_element(
    lines_total_prices, total_price, total_discount_amount, currency
):
    """Calculate the discount for last element.
    If the given line is last on the list we should calculate the discount by difference
    between total discount amount and sum of discounts applied to rest of the lines,
    otherwise the sum of discounts won't be equal to the discount amount.
    """
    sum_of_discounts_other_elements = sum(
        [
            quantize_price(
                line_total_price / total_price * total_discount_amount,
                currency,
            )
            for line_total_price in lines_total_prices
        ]
    )
    return total_discount_amount - sum_of_discounts_other_elements
