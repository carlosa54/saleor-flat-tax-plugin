from decimal import Decimal
from prices import Money, MoneyRange, TaxedMoney, TaxedMoneyRange, flat_tax

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

    final_tax_rate = Decimal(float(rate) / 100)

    def tax(base, keep_gross=False):
        return flat_tax(base, final_tax_rate, keep_gross=keep_gross)

    return tax
