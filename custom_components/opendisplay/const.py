"""Constants for the OpenDisplay integration."""

DOMAIN = "opendisplay"
CONF_ENCRYPTION_KEY = "encryption_key"
CONF_CACHED_DEVICE_CONFIG = "cached_device_config"
CONF_CACHED_FIRMWARE = "cached_firmware"
CONF_CACHED_IS_FLEX = "cached_is_flex"
CONF_CACHED_LAST_SEEN = "cached_last_seen"
CONF_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES = "deep_sleep_timeout_margin_minutes"
SIGNAL_IMAGE_UPDATED = f"{DOMAIN}_image_updated"
SIGNAL_DEVICE_SEEN = f"{DOMAIN}_device_seen"
SIGNAL_PENDING_UPLOAD = f"{DOMAIN}_pending_upload"

DEFAULT_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES = 7
MIN_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES = 0
MAX_DEEP_SLEEP_TIMEOUT_MARGIN_MINUTES = 24 * 60
