"""Constants for the OpenDisplay integration."""

DOMAIN = "opendisplay"
CONF_ENCRYPTION_KEY = "encryption_key"
SIGNAL_IMAGE_UPDATED = f"{DOMAIN}_image_updated"
# Fallback expiry (seconds) used when the device reports deep_sleep_time_seconds = 0
DEFAULT_DEEP_SLEEP_EXPIRY_SECONDS = 14400  # 4 hours
