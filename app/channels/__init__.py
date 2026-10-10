from collections.abc import Callable
from datetime import datetime
from typing import Protocol

import httpx

from app.config import Settings
from app.models import ChannelOrder, PushResult, SkuMapping


class Channel(Protocol):
    name: str

    async def fetch_orders(self, since: datetime, is_known: Callable[[str], bool]) -> list[ChannelOrder]:
        """Return new orders placed since `since`, skipping order ids for which is_known(id) is True."""
        ...

    async def push_stock(self, items: list[tuple[SkuMapping, int]]) -> list[PushResult]: ...


def build_channels(settings: Settings, http: httpx.AsyncClient) -> dict[str, Channel]:
    from app.channels.amazon import AmazonChannel
    from app.channels.flipkart import FlipkartChannel

    channels: dict[str, Channel] = {}
    if settings.flipkart_enabled:
        channels["flipkart"] = FlipkartChannel(settings, http)
    if settings.amazon_enabled:
        channels["amazon"] = AmazonChannel(settings, http)
    return channels
