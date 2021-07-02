import itertools
from dataclasses import dataclass
from functools import singledispatchmethod  # type: ignore[attr-defined]
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    Iterable,
    List,
    Optional,
    Protocol,
    Union,
    cast,
)

from prices import TaxedMoney

from saleor.warehouse import WarehouseClickAndCollectOption

from ..shipping.models import ShippingMethod, ShippingMethodChannelListing
from ..warehouse.models import Warehouse
from . import base_calculations

if TYPE_CHECKING:
    from ..account.models import Address, User
    from ..channel.models import Channel
    from ..discount import DiscountInfo
    from ..plugins.manager import PluginsManager
    from ..product.models import (
        Collection,
        Product,
        ProductType,
        ProductVariant,
        ProductVariantChannelListing,
    )
    from .models import Checkout, CheckoutLine


# Due to mypy bug (paste ticket)
class ShippingCalculation(Protocol):
    def __call__(self, checkout_info: "CheckoutInfo", lines: Any) -> TaxedMoney:
        ...


@dataclass
class CheckoutLineInfo:
    line: "CheckoutLine"
    variant: "ProductVariant"
    channel_listing: "ProductVariantChannelListing"
    product: "Product"
    product_type: "ProductType"
    collections: List["Collection"]


@dataclass
class CheckoutInfo:
    checkout: "Checkout"
    user: Optional["User"]
    channel: "Channel"
    billing_address: Optional["Address"]
    shipping_address: Optional["Address"]
    shipping_method: Optional["ShippingMethod"]  # Will be deprecated
    delivery_method_info: "DeliveryMethodInfo"
    valid_shipping_methods: List["ShippingMethod"]
    valid_pick_up_points: List["Warehouse"]
    shipping_method_channel_listings: Optional[ShippingMethodChannelListing]

    @property
    def valid_delivery_methods(self) -> List[Union["ShippingMethod", "Warehouse"]]:
        return list(
            itertools.chain(self.valid_shipping_methods, self.valid_pick_up_points)
        )

    def get_country(self) -> str:
        address = self.shipping_address or self.billing_address
        if address is None or not address.country:
            return self.checkout.country.code
        return address.country.code

    def get_customer_email(self) -> str:
        return self.user.email if self.user else self.checkout.email


@dataclass(frozen=True)
class DeliveryMethodInfo:
    is_click_and_collect: bool
    is_local_collection_point: bool
    delivery_method: Optional[Union["ShippingMethod", "Warehouse"]]
    shipping_address: Optional["Address"]
    shipping_calculation_strategy: ShippingCalculation

    @singledispatchmethod
    @classmethod
    def from_delivery_method(cls, delivery_method, shipping_address):
        raise NotImplementedError("Incompatible Type")

    @from_delivery_method.register(ShippingMethod)
    @from_delivery_method.register(type(None))
    @classmethod
    def _(cls, delivery_method, shipping_address):

        shipping_calculation_strategy = base_calculations.base_checkout_shipping_price
        return cls(
            is_click_and_collect=False,
            is_local_collection_point=False,
            shipping_address=shipping_address,
            delivery_method=delivery_method,
            shipping_calculation_strategy=shipping_calculation_strategy,
        )

    @from_delivery_method.register(Warehouse)  # type: ignore[no-redef]
    @classmethod
    def _(cls, delivery_method, _):
        is_local_collection_point = (
            delivery_method.click_and_collect_option
            == WarehouseClickAndCollectOption.LOCAL_STOCK
        )

        shipping_calculation_strategy = (
            base_calculations.base_checkout_shipping_price_click_and_collect
        )
        return cls(
            is_click_and_collect=True,
            is_local_collection_point=is_local_collection_point,
            delivery_method=delivery_method,
            shipping_address=delivery_method.address,
            shipping_calculation_strategy=shipping_calculation_strategy,
        )

    def get_warehouse_filter_lookup(self) -> Dict[str, Any]:
        return (
            {"warehouse__pk": self.delivery_method.pk}
            if self.is_local_collection_point and self.delivery_method is not None
            else {}
        )


def fetch_checkout_lines(checkout: "Checkout") -> Iterable[CheckoutLineInfo]:
    """Fetch checkout lines as CheckoutLineInfo objects."""
    lines = checkout.lines.prefetch_related(
        "variant__product__collections",
        "variant__channel_listings__channel",
        "variant__product__product_type",
    )
    lines_info = []

    for line in lines:
        variant = line.variant
        product = variant.product
        product_type = product.product_type
        collections = list(product.collections.all())

        variant_channel_listing = None
        for channel_listing in line.variant.channel_listings.all():
            if channel_listing.channel_id == checkout.channel_id:
                variant_channel_listing = channel_listing

        # FIXME: Temporary solution to pass type checks. Figure out how to handle case
        # when variant channel listing is not defined for a checkout line.
        if not variant_channel_listing:
            continue

        lines_info.append(
            CheckoutLineInfo(
                line=line,
                variant=variant,
                channel_listing=variant_channel_listing,
                product=product,
                product_type=product_type,
                collections=collections,
            )
        )
    return lines_info


def fetch_checkout_info(
    checkout: "Checkout",
    lines: Iterable[CheckoutLineInfo],
    discounts: Iterable["DiscountInfo"],
    manager: "PluginsManager",
) -> CheckoutInfo:
    """Fetch checkout as CheckoutInfo object."""

    channel = checkout.channel
    shipping_address = checkout.shipping_address
    shipping_method = checkout.shipping_method
    shipping_channel_listings = ShippingMethodChannelListing.objects.filter(
        shipping_method=shipping_method, channel=channel
    ).first()
    delivery_method = checkout.collection_point or shipping_method
    delivery_method_info = DeliveryMethodInfo.from_delivery_method(
        delivery_method, shipping_address
    )
    checkout_info = CheckoutInfo(
        checkout=checkout,
        user=checkout.user,
        channel=channel,
        billing_address=checkout.billing_address,
        shipping_address=shipping_address,
        shipping_method=shipping_method,
        delivery_method_info=delivery_method_info,
        shipping_method_channel_listings=shipping_channel_listings,
        valid_shipping_methods=[],
        valid_pick_up_points=[],
    )

    valid_shipping_methods = get_valid_shipping_method_list_for_checkout_info(
        checkout_info, shipping_address, lines, discounts, manager
    )
    valid_pick_up_points = get_valid_collection_points_for_checkout_info(
        checkout_info, lines
    )
    checkout_info.valid_shipping_methods = valid_shipping_methods
    checkout_info.valid_pick_up_points = valid_pick_up_points
    checkout_info.delivery_method_info = delivery_method_info

    return checkout_info


def update_checkout_info_shipping_address(
    checkout_info: CheckoutInfo,
    address: Optional["Address"],
    lines: Iterable[CheckoutLineInfo],
    discounts: Iterable["DiscountInfo"],
    manager: "PluginsManager",
):
    checkout_info.shipping_address = address
    valid_methods = get_valid_shipping_method_list_for_checkout_info(
        checkout_info, address, lines, discounts, manager
    )
    checkout_info.valid_shipping_methods = valid_methods
    delivery_method = checkout_info.delivery_method_info.delivery_method
    checkout_info.delivery_method_info = DeliveryMethodInfo.from_delivery_method(
        delivery_method, address
    )


def get_valid_shipping_method_list_for_checkout_info(
    checkout_info: "CheckoutInfo",
    shipping_address: Optional["Address"],
    lines: Iterable[CheckoutLineInfo],
    discounts: Iterable["DiscountInfo"],
    manager: "PluginsManager",
):
    from .utils import get_valid_shipping_methods_for_checkout

    country_code = shipping_address.country.code if shipping_address else None
    subtotal = manager.calculate_checkout_subtotal(
        checkout_info, lines, checkout_info.shipping_address, discounts
    )
    valid_shipping_method = get_valid_shipping_methods_for_checkout(
        checkout_info, lines, subtotal, country_code=country_code
    )
    valid_shipping_method = (
        list(valid_shipping_method) if valid_shipping_method is not None else []
    )
    return valid_shipping_method


def get_valid_collection_points_for_checkout_info(
    checkout_info: "CheckoutInfo",
    lines: Iterable[CheckoutLineInfo],
):
    from .utils import get_valid_collection_points_for_checkout

    valid_collection_points = get_valid_collection_points_for_checkout(lines)
    return list(valid_collection_points) if valid_collection_points is not None else []


def update_checkout_info_shipping_method(
    checkout_info: CheckoutInfo, shipping_method: Optional["ShippingMethod"]
):
    checkout_info.shipping_method = shipping_method
    checkout_info.shipping_method_channel_listings = (
        (
            ShippingMethodChannelListing.objects.filter(
                shipping_method=shipping_method, channel=checkout_info.channel
            ).first()
        )
        if shipping_method
        else None
    )


def update_checkout_info_delivery_method(
    checkout_info: CheckoutInfo,
    delivery_method: Optional[Union["ShippingMethod", "Warehouse"]],
):
    checkout_info.delivery_method_info = DeliveryMethodInfo.from_delivery_method(
        delivery_method, checkout_info.shipping_address
    )
    if not checkout_info.delivery_method_info.is_click_and_collect:
        shipping_method = cast(
            ShippingMethod, checkout_info.delivery_method_info.delivery_method
        )
        checkout_info.shipping_method_channel_listings = (
            (
                ShippingMethodChannelListing.objects.filter(
                    shipping_method=shipping_method, channel=checkout_info.channel
                ).first()
            )
            if delivery_method
            else None
        )
