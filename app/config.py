from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables or the .env file."""

    # env_ignore_empty: a blank line like "PORTAL_ORDER_FOR_USER_ID=" means "use the default"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    database_path: str = "data/sync.db"
    admin_token: str = ""  # required in X-Admin-Token for admin endpoints; empty = admin endpoints disabled

    # Sync behaviour
    scheduler_enabled: bool = True
    poll_interval_seconds: int = 300
    reconcile_interval_seconds: int = 6 * 3600
    lookback_hours: int = 48
    dry_run: bool = True  # compute and log stock pushes, but never send them to Amazon/Flipkart
    stock_buffer: int = 0  # units held back from marketplaces to avoid overselling

    # Dealer portal: "local" (built-in test store), "rest" (Vagmine order flow),
    # or "api" (forward orders to ORDER_API_URL; stock comes from POST /api/v1/stock)
    portal_mode: str = "local"
    order_api_url: str = ""  # every new marketplace order is POSTed here (PORTAL_MODE=api)
    order_api_token: str = ""  # optional; sent as "Authorization: Bearer ..."
    portal_base_url: str = ""
    portal_webhook_secret: str = ""

    # Dealer portal login: access token is sent as "Authorization: Bearer ...", refreshed with the refresh token
    portal_username: str = ""
    portal_password: str = ""
    portal_login_path: str = "/api/v1/auth/login"
    portal_refresh_path: str = "/api/v1/auth/refresh"
    portal_username_field: str = "username"  # login body: {<username_field>: ..., "password": ...}
    portal_refresh_field: str = "refresh_token"  # refresh body: {<refresh_field>: ...}
    portal_token_ttl_seconds: int = 900  # used only if neither the login response nor the JWT has an expiry

    # Dealer portal (Vagmine) order flow: resolve customer -> analyze -> push order -> track
    portal_write_enabled: bool = False  # false = resolve/push are blocked, so nothing is created on the portal
    portal_resolve_path: str = "/api/v1/PUSH_ORDER/customer/resolve"
    portal_analyze_path: str = "/api/v1/PUSH_ORDER/analyze"
    portal_push_path: str = "/api/v1/PUSH_ORDER"
    portal_track_path: str = ""  # e.g. /api/v1/PUSH_ORDER/track/{order_reference_no}; empty until the docs arrive
    portal_source_branch_dealer_id: int | None = None
    portal_home_branch_dealer_id: int | None = None
    portal_order_for_user_id: int | None = None

    # Flipkart Seller API (Seller Hub > Manage Profile > Developer Access > Self Access)
    flipkart_enabled: bool = False
    flipkart_app_id: str = ""
    flipkart_app_secret: str = ""
    flipkart_location_id: str = ""
    flipkart_base_url: str = "https://api.flipkart.net"

    # Amazon SP-API (Solution Provider Portal > Developer Central)
    amazon_enabled: bool = False
    amazon_client_id: str = ""
    amazon_client_secret: str = ""
    amazon_refresh_token: str = ""
    amazon_seller_id: str = ""
    amazon_marketplace_id: str = "A21TJRUUN4KGV"  # amazon.in
    amazon_endpoint: str = "https://sellingpartnerapi-eu.amazon.com"  # India is served by the EU endpoint


@lru_cache
def get_settings() -> Settings:
    return Settings()
