"""Constants for the OpenDisplay integration."""

from datetime import timedelta

DOMAIN = "opendisplay"
CONF_ENCRYPTION_KEY = "encryption_key"

# --- WiFi (LAN) transport ---------------------------------------------------
# Entry-data keys for a WiFi-reachable device (protocol 2.2 SECTION 9). Reuse
# the Home Assistant const string values ("host"/"port") so the keys are
# interchangeable with homeassistant.const.CONF_HOST/CONF_PORT; CONF_TLS stores
# the mDNS ``tls`` flag so the resolver knows which channel (plaintext vs
# TLS-PSK) the device is serving.
CONF_HOST = "host"
CONF_PORT = "port"
CONF_TLS = "tls"

# Default LAN ports (SECTION 9). Plaintext is the configured base port; TLS is
# derived as base + 1. Clients learn the live port from the mDNS SRV record.
DEFAULT_LAN_PORT = 2446
DEFAULT_TLS_PORT = 2447

# Prefer WiFi only when the device was seen via mDNS within this window. The SRV
# record TTL is 120 s, so 10 min gives comfortable margin: a device that has
# stopped announcing (powered off, left the network) ages out and delivery
# falls back to BLE rather than stalling on a dead TCP connect.
MDNS_FRESHNESS_WINDOW = timedelta(minutes=10)
MDNS_FRESHNESS_WINDOW_S = MDNS_FRESHNESS_WINDOW.total_seconds()

# TCP connect timeout for the config-flow reachability probe and the delivery
# WiFi attempt. Short: a reachable LAN device connects in well under a second,
# so 5 s only bounds an unreachable/stale host before falling back to BLE.
TCP_CONNECT_TIMEOUT_S = 5.0

# Dispatcher signals (suffixed with the device address at send/subscribe time).
# Carries the JPEG preview of the frame shown by the image entity.
SIGNAL_IMAGE_UPDATED = f"{DOMAIN}_image_updated"
# Carries a DeliverySnapshot describing the delivery manager's pending state
# (see delivery.py). Consumed by the image entity and the "update pending"
# binary sensor.
SIGNAL_PENDING_STATE = f"{DOMAIN}_pending_state"

# --- Options (options flow) -------------------------------------------------
# Deep-sleep handling mode: follow the device power config, or force on/off.
CONF_SLEEP_MODE = "sleep_mode"
SLEEP_MODE_AUTO = "auto"
SLEEP_MODE_ON = "on"
SLEEP_MODE_OFF = "off"
DEFAULT_SLEEP_MODE = SLEEP_MODE_AUTO

# Consecutive expected wakes that may be missed before entities go unavailable.
CONF_MISSED_CYCLES = "missed_cycles"
DEFAULT_MISSED_CYCLES = 3

# Hours a queued upload survives before expiring (safely above the 18 h max
# firmware sleep interval).
CONF_QUEUE_TIMEOUT_HOURS = "queue_timeout_hours"
DEFAULT_QUEUE_TIMEOUT_HOURS = 24

# Wall-clock ceilings on the two interactive active-connect paths. Unlike the
# delivery deadline (DELIVERY_DEADLINE_S, sized for a full image stream), these
# bound a lightweight connect + interrogate + firmware read so a wedged BLE link
# (stalled notify subscription or a GATT write stuck at a proxy) surfaces as a
# typed error instead of hanging the user (config-flow dialog) or setup forever.
CONNECT_PROBE_DEADLINE_S = 45.0  # config-flow connection probe (user waiting)
SETUP_DEADLINE_S = 60.0  # entry-setup active connect (has sleepy-cache fallback)

# Probe a probably-asleep device with one short connect attempt before queuing
# an image send. Catches wake adverts the scanner missed and Silabs tags that
# advertise continuously despite a battery+deep-sleep power config. Bounded
# cost: one attempt with a short timeout (see services.PROBE_CONNECT_TIMEOUT_S).
CONF_PROBE_BEFORE_QUEUE = "probe_before_queue"
DEFAULT_PROBE_BEFORE_QUEUE = True

# BLE sliding-window pipe transfer (py-opendisplay pipe client). Frames sent
# per acknowledgment (QUIC-style N) and maximum upload frames in flight
# (window W). max_queue_size == 1 disables fast transfer (legacy
# stop-and-wait only).
CONF_BLOCKS_PER_ACK = "blocks_per_ack"
DEFAULT_BLOCKS_PER_ACK = 4

CONF_MAX_QUEUE_SIZE = "max_queue_size"
DEFAULT_MAX_QUEUE_SIZE = 16

# --- Cache ------------------------------------------------------------------
# entry.data key holding the serialized device state used to set up the entry
# without connecting when a sleepy device is dark at startup.
CONF_CACHED_STATE = "cached_state"

# --- Bus events -------------------------------------------------------------
EVENT_CONTENT_DELIVERED = f"{DOMAIN}_content_delivered"
EVENT_CONTENT_EXPIRED = f"{DOMAIN}_content_expired"
# Fired when a queued upload is dropped because the device's live display
# config no longer matches the config the image was prepared against.
EVENT_CONTENT_CONFIG_MISMATCH = f"{DOMAIN}_content_config_mismatch"
