from pydantic import BaseModel, Field


class SkuMapping(BaseModel):
    """Links one dealer-portal SKU to its listing on each marketplace."""

    portal_sku: str
    amazon_sku: str | None = None
    flipkart_sku: str | None = None
    flipkart_fsn: str | None = None  # Flipkart product_id, required for inventory updates
    active: bool = True

    def channel_sku(self, channel: str) -> str | None:
        return {"amazon": self.amazon_sku, "flipkart": self.flipkart_sku}.get(channel)


class OrderLine(BaseModel):
    channel_sku: str
    quantity: int
    sku: str | None = None  # dealer-portal SKU, filled in from the SKU map
    line_total: float | None = None
    title: str | None = None
    channel_line_id: str | None = None


class ChannelOrder(BaseModel):
    """A marketplace order normalised into one shape for the dealer portal."""

    channel: str
    channel_order_id: str
    status: str
    ordered_at: str | None = None
    seller_fulfilled: bool = True
    currency: str = "INR"
    total: float | None = None
    shipping_address: dict | None = None
    lines: list[OrderLine] = Field(default_factory=list)


class PushResult(BaseModel):
    portal_sku: str
    quantity: int
    ok: bool
    message: str = ""


# --- Stock API (POST /api/v1/stock) ----------------------------------------------


class StockItemIn(BaseModel):
    sku: str = Field(min_length=1, description="Your SKU / part number (portal_sku in the SKU map)")
    quantity: int = Field(ge=0)


class StockIn(BaseModel):
    items: list[StockItemIn] = Field(min_length=1, max_length=5000)


# --- Dealer portal order flow (request bodies of our /api/v1 endpoints) ---------


class CustomerIn(BaseModel):
    external_customer_id: str = Field(min_length=1, description="Stable id from the sales channel, e.g. 'amazon:9876543210'. Sent to the portal as shopify_customer_id.")
    name: str = Field(min_length=1)
    phone: str | None = None
    email: str | None = None
    address: str | None = None
    gst_no: str | None = None
    home_branch_dealer_id: int | None = Field(default=None, description="Defaults to PORTAL_HOME_BRANCH_DEALER_ID")


class OrderItemIn(BaseModel):
    part_no: str = Field(min_length=1)
    quantity: int = Field(gt=0)


class OrderIn(BaseModel):
    order_reference_no: str = Field(min_length=1, max_length=100)
    items: list[OrderItemIn] = Field(min_length=1)
    customer_account_id: int
    order_for_user_id: int | None = Field(default=None, description="Defaults to PORTAL_ORDER_FOR_USER_ID")
    source_branch_dealer_id: int | None = Field(default=None, description="Defaults to PORTAL_SOURCE_BRANCH_DEALER_ID")


class ProcessOrderIn(BaseModel):
    """Full flow in one call: resolve customer -> analyze -> push order."""

    order_reference_no: str = Field(min_length=1, max_length=100)
    customer: CustomerIn
    items: list[OrderItemIn] = Field(min_length=1)
    order_for_user_id: int | None = None
    source_branch_dealer_id: int | None = None
