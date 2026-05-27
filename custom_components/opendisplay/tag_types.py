from __future__ import annotations

import os

import aiohttp
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage
from .const import FALLBACK_TAG_DEFINITIONS

_LOGGER = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com/repos/OpenEPaperLink/OpenEPaperLink/contents/resources/tagtypes"
GITHUB_RAW_URL = "https://raw.githubusercontent.com/OpenEPaperLink/OpenEPaperLink/master/resources/tagtypes"
CACHE_DURATION = timedelta(hours=48)  # Cache tag definitions for 48 hours
STORAGE_VERSION = 1
STORAGE_KEY = "opendisplay_tagtypes"
LEGACY_STORAGE_KEY = "open_display_tagtypes"
LEGACY_TAG_TYPES_FILE = "open_display_tagtypes.json"


class TagType:
    """Represents a specific tag hardware type and its capabilities.

    Encapsulates all the hardware-specific properties of a tag model, including:

    - Display dimensions and color capabilities
    - Buffer format and rotation settings
    - LUT (Look-Up Table) configuration
    - Content type compatibility

    This information is used for proper image generation and rendering
    to ensure content displays correctly on different tag models.

    Attributes:
        type_id: Numeric identifier for the tag type
        version: Format version of the tag type definition
        name: Human-readable name of the tag model
        width: Display width in pixels
        height: Display height in pixels
        rotatebuffer: Buffer rotation setting (0=none, 1=90°, 2=180°, 3=270°)
        bpp: Bits per pixel (color depth)
        color_table: Mapping of color names to RGB values
        short_lut: Short LUT configuration
        options: Additional tag options
        content_ids: Compatible content IDs
        template: Template configuration
        use_template: Template usage settings
        zlib_compression: Compression settings
    """

    def __init__(self, type_id: int, data: dict):
        """Initialize a tag type from type ID and properties.

        Creates a TagType instance by mapping properties from the
        provided data dictionary to class attributes, with defaults
        for missing properties.

        Args:
            type_id: Numeric identifier for this tag type
            data: Dictionary containing tag properties from GitHub or storage
        """
        self.type_id = type_id
        self.version = data.get('version', 1)
        self.name = data.get('name', f"Unknown Type {type_id}")
        self.width = data.get('width', 296)
        self.height = data.get('height', 128)
        self.rotatebuffer = data.get('rotatebuffer', 0)
        self.bpp = data.get('bpp', 2)
        self.color_table = data.get('colortable', {
            'white': [255, 255, 255],
            'black': [0, 0, 0],
            'red': [255, 0, 0],
        })
        self.short_lut = data.get('shortlut', 2)
        self.options = data.get('options', [])
        self.content_ids = data.get('contentids', [])
        self.template = data.get('template', {})
        self.use_template = data.get('usetemplate', None)
        self.zlib_compression = data.get('zlib_compression', None)
        self._raw_data = data

    def to_dict(self) -> dict:
        """Convert TagType instance to a serializable dictionary.

        Creates a dictionary representation of the tag type suitable for
        storage. This is used when saving to persistent storage.

        Returns:
            dict: Dictionary containing all tag type properties
        """
        return {
            'version': self.version,
            'name': self.name,
            'width': self.width,
            'height': self.height,
            'rotatebuffer': self.rotatebuffer,
            'bpp': self.bpp,
            'colortable': self.color_table,
            'shortlut': self.short_lut,
            'options': list(self.options),
            'contentids': list(self.content_ids),
            'template': self.template,
            'usetemplate': self.use_template,
            'zlib_compression': self.zlib_compression,
        }

    @classmethod
    def from_dict(cls, type_id: int, data: dict) -> TagType:
        """Create TagType from stored dictionary.

        Factory method to reconstruct a TagType instance from a previously
        serialized dictionary when loaded from persistent storage.

        Args:
            type_id: Numeric identifier for this tag type
            data: Dictionary containing serialized tag type properties

        Returns:
            TagType: Reconstructed tag type instance
        """
        raw_data = {
            'version': data.get('version', 1),
            'name': data.get('name'),
            'width': data.get('width'),
            'height': data.get('height'),
            'rotatebuffer': data.get('rotatebuffer'),
            'bpp': data.get('bpp'),
            'shortlut': data.get('short_lut', data.get('shortlut')),
            'colortable': data.get('colortable'),
            'options': data.get('options', []),
            'contentids': data.get('contentids', data.get('content_ids', [])),
            'template': data.get('template', {}),
            'usetemplate': data.get('usetemplate'),
            'zlib_compression': data.get('zlib_compression', None),
        }
        return cls(type_id, raw_data)

    def get(self, attr: str, default: Any = None) -> Any:
        """Get attribute value, supporting dict-like access.

        Provides dictionary-style access to tag type attributes,
        with a default value if the attribute doesn't exist.

        Args:
            attr: Name of the attribute to retrieve
            default: Value to return if attribute doesn't exist

        Returns:
            Any: The attribute value or default if not found
        """
        return getattr(self, attr, default)


class TagTypesManager:
    """Manages tag type definitions fetched from GitHub.

    Handles loading, caching, and refreshing tag type definitions from
    the OpenDisplay GitHub repository. Provides local storage to
    avoid frequent network requests and fallback definitions for
    when GitHub is unreachable.

    The manager is implemented as a quasi-singleton through the
    get_tag_types_manager function to ensure consistent state
    across the integration.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the tag types manager.

        Sets up the manager with empty state and configuration paths
        derived from the Home Assistant instance.

        Args:
            hass: Home Assistant instance for storage access
        """
        self._hass = hass
        self._tag_types: Dict[int, TagType] = {}
        self._last_update: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._legacy_storage_file = self._hass.config.path(LEGACY_TAG_TYPES_FILE)
        self._store = storage.Store(
            hass,
            version=STORAGE_VERSION,
            key=STORAGE_KEY,
        )
        self._legacy_store = storage.Store(
            hass,
            version=STORAGE_VERSION,
            key=LEGACY_STORAGE_KEY,
        )
        _LOGGER.debug("TagTypesManager instance created")

    async def load_stored_data(self) -> None:
        """Load stored tag type definitions from disk.

        Attempts to load previously cached tag type definitions from the
        Home Assistant storage helper. If valid data is found, it's used to
        populate the manager's state. Otherwise, a fresh fetch from GitHub
        is initiated and the legacy file in the config directory is removed.

        This helps reduce network requests and provides offline operation capability.
        """
        stored_data: dict[str, Any] | None = None
        try:
            stored_data = await self._store.async_load()
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.error("Error loading tag types from storage: %s", err, exc_info=True)

        if stored_data:
            if stored_data.get("version") == STORAGE_VERSION:
                await self._load_from_payload(stored_data)
                return
            _LOGGER.warning("Stored tag types version mismatch, refetching fresh definitions")

        if not stored_data:
            legacy_data: dict[str, Any] | None = None
            try:
                legacy_data = await self._legacy_store.async_load()
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.error("Error loading legacy tag types from storage: %s", err, exc_info=True)

            if legacy_data and legacy_data.get("version") == STORAGE_VERSION:
                await self._load_from_payload(legacy_data)
                await self._save_to_store()
                try:
                    await storage.async_remove_store(self._hass, LEGACY_STORAGE_KEY)
                except Exception as err:  # pragma: no cover - defensive
                    _LOGGER.warning("Failed to remove legacy tag types storage: %s", err)
                return

        fetch_success = await self._fetch_tag_types()
        if fetch_success:
            await self._cleanup_legacy_file()
        else:
            # If fetch failed and we have no types, load fallback definitions
            if not self._tag_types:
                _LOGGER.warning(
                    "Failed to fetch tag types from GitHub and no stored data available. "
                    "Loading fallback definitions. Tag types will be refreshed on next integration reload."
                )
                self._load_fallback_types()
            await self._cleanup_legacy_file()

    async def _save_to_store(self) -> None:
        """Persist tag types using Home Assistant storage helper."""
        if not self._last_update:
            self._last_update = datetime.now()

        data = {
            "version": STORAGE_VERSION,
            "last_update": self._last_update.isoformat(),
            "tag_types": {
                str(type_id): tag_type.to_dict()
                for type_id, tag_type in self._tag_types.items()
            },
        }

        try:
            await self._store.async_save(data)
        except Exception as err:  # pragma: no cover - storage helper handles atomicity
            _LOGGER.error("Error saving tag types to storage: %s", err)

    async def _load_from_payload(self, stored_data: dict[str, Any]) -> None:
        """Populate tag types from stored payload."""
        try:
            last_update = stored_data.get("last_update")
            self._last_update = (
                datetime.fromisoformat(last_update) if last_update else datetime.now()
            )
        except (TypeError, ValueError):
            self._last_update = datetime.now()

        self._tag_types = {}
        for type_id_str, type_data in stored_data.get("tag_types", {}).items():
            try:
                type_id = int(type_id_str)
                self._tag_types[type_id] = TagType.from_dict(type_id, type_data)
                _LOGGER.debug(
                    "Loaded tag type %d: %s", type_id, self._tag_types[type_id].name
                )
            except Exception as err:  # pragma: no cover - defensive
                _LOGGER.error("Error loading tag type %s: %s", type_id_str, err)

        _LOGGER.info("Loaded %d tag types from storage", len(self._tag_types))

    async def _cleanup_legacy_file(self) -> None:
        """Remove legacy tag types file from config directory."""

        def _remove() -> bool:
            if os.path.exists(self._legacy_storage_file):
                os.remove(self._legacy_storage_file)
                return True
            return False

        try:
            removed = await self._hass.async_add_executor_job(_remove)
            if removed:
                _LOGGER.info("Migrated tag types to Home Assistant storage; legacy file removed")
        except OSError as err:
            _LOGGER.error("Error removing legacy tag types file: %s", err)

    async def ensure_types_loaded(self) -> None:
        """Ensure tag types are loaded and not too old.

        Checks if tag types are already loaded and recent enough.
        If not loaded or older than CACHE_DURATION, initiates a refresh from GitHub.

        This is the primary method that should be called before accessing
        tag type information to ensure data availability.

        If tag types cannot be loaded from GitHub or storage, fallback
        definitions will be used to ensure basic functionality.
        """
        async with self._lock:
            if not self._tag_types:
                await self.load_stored_data()

            # After load_stored_data, we should always have types (either from storage,
            # GitHub, or fallback). If not, something is seriously wrong.
            if not self._tag_types:
                _LOGGER.error(
                    "Critical error: No tag types available after loading. "
                    "This should not happen as fallback types should be loaded."
                )
                # Load fallback as last resort
                self._load_fallback_types()

            # If the cache is expired, attempt refresh
            if not self._last_update or datetime.now() - self._last_update > CACHE_DURATION:
                _LOGGER.debug("Tag types cache expired, attempting refresh")
                fetch_success = await self._fetch_tag_types()

                # If refresh failed, log a warning but continue with existing types
                if not fetch_success:
                    _LOGGER.warning(
                        "Failed to refresh tag types from GitHub. Using cached or fallback definitions."
                    )

    async def _fetch_tag_types(self) -> bool:
        """Fetch tag type definitions from GitHub.

        Retrieves tag type definitions from the OpenDisplay GitHub repository:

        1. Queries the GitHub API to list available definition files
        2. Downloads each file and parses as JSON
        3. Validates the definition contains required fields
        4. Creates TagType instances from valid definitions

        If fetching fails and no existing definitions are available,
        falls back to built-in basic definitions.
        """
        try:
            _LOGGER.debug("Fetching tag type definitions from GitHub: %s", GITHUB_API_URL)
            async with aiohttp.ClientSession() as session:
                # First get the directory listing from GitHub API
                headers = {"Accept": "application/vnd.github.v3+json"}
                async with session.get(GITHUB_API_URL, headers=headers) as response:
                    if response.status != 200:
                        _LOGGER.error(
                            "GitHub API request failed with status %d for URL: %s",
                            response.status,
                            GITHUB_API_URL
                        )
                        raise Exception(f"GitHub API returned status {response.status}")

                    directory_contents = await response.json()

                    # Filter for .json files and extract type IDs
                    type_files = []
                    for item in directory_contents:
                        if item["name"].endswith(".json"):
                            # Try to extract type ID from filename
                            try:
                                base_name = item["name"][:-5]  # Remove .json extension
                                try:
                                    type_id = int(base_name, 16)
                                    _LOGGER.debug(f"Parsed hex type ID {base_name} -> {type_id}")
                                    type_files.append((type_id, item["download_url"]))
                                    continue
                                except ValueError:
                                    pass

                                # If not hex, try decimal
                                try:
                                    type_id = int(base_name)
                                    _LOGGER.debug(f"Parsed decimal type ID {base_name} -> {type_id}")
                                    type_files.append((type_id, item["download_url"]))
                                    continue
                                except ValueError:
                                    pass
                                _LOGGER.warning(f"Could not parse type ID from filename: {item['name']}")

                            except Exception as e:
                                _LOGGER.warning(f"Error processing filename {item['name']}: {str(e)}")

                # Now fetch all found definitions
                new_types = {}
                for hw_type, url in type_files:
                    try:
                        async with session.get(url) as response:
                            if response.status == 200:
                                text_content = await response.text()
                                try:
                                    data = json.loads(text_content)
                                    if self._validate_tag_definition(data):
                                        new_types[hw_type] = TagType(hw_type, data)
                                        _LOGGER.debug(f"Loaded tag type {hw_type}: {data['name']}")
                                except json.JSONDecodeError:
                                    _LOGGER.error(f"Invalid JSON in tag type {hw_type}")
                    except Exception as e:
                        _LOGGER.error(f"Error loading tag type {hw_type}: {str(e)}")

                if new_types:
                    self._tag_types = new_types
                    self._last_update = datetime.now()
                    _LOGGER.info(
                        "Successfully loaded %d tag definitions from GitHub",
                        len(new_types)
                    )
                    await self._save_to_store()
                    return True
                _LOGGER.warning(
                    "No valid tag definitions found in GitHub repository at %s",
                    GITHUB_API_URL
                )

        except Exception as e:
            _LOGGER.error(
                "Error fetching tag types from %s: %s",
                GITHUB_API_URL,
                str(e),
                exc_info=True
            )
            return False

        # Do NOT load fallback types - let caller decide how to handle failure
        return False

    def _validate_tag_definition(self, data: Dict) -> bool:
        """Validate that a tag definition has required fields.

        Checks if the tag definition dictionary contains all required fields
        to be considered valid. A valid definition must include:

        - version: Tag type format version
        - name: Human-readable model name
        - width: Display width in pixels
        - height: Display height in pixels

        Args:
            data: Dictionary containing tag type definition

        Returns:
            bool: True if the definition is valid, False otherwise
        """
        required_fields = {'version', 'name', 'width', 'height'}
        return all(field in data for field in required_fields)

    def _load_fallback_types(self) -> None:
        """Load basic fallback definitions if fetching fails on first run.

        Populates the manager with a comprehensive set of built-in tag type
        definitions to ensure basic functionality when GitHub is unreachable.

        This provides support for all known tag models with proper dimensions,
        version information, and basic configuration options.

        The fallback types include all tag definitions from the OpenEPaperLink
        repository at: https://github.com/OpenEPaperLink/OpenEPaperLink/tree/master/resources/tagtypes
        """
        self._tag_types = {
            type_id: TagType(type_id, data) for type_id, data in FALLBACK_TAG_DEFINITIONS.items()
        }
        self._last_update = datetime.now()
        _LOGGER.warning("Loaded fallback tag definitions")

    async def get_tag_info(self, hw_type: int) -> TagType:
        """Get tag information for a specific hardware type.

        Retrieves the TagType instance for the specified hardware type,
        ensuring type definitions are loaded first if needed.

        This method should be used to get tag information
        when processing tag data from the AP.

        Args:
            hw_type: Hardware type ID number

        Returns:
            TagType: Tag type definition object

        Raises:
            KeyError: If the hardware type is unknown
        """
        await self.ensure_types_loaded()
        tag_def = self._tag_types[hw_type]
        return tag_def

    def get_hw_dimensions(self, hw_type: int) -> Tuple[int, int]:
        """Get width and height for a hardware type.

        Returns the display dimensions for the specified tag type.
        If the type is unknown, returns safe default values.

        Args:
            hw_type: Hardware type ID number

        Returns:
            Tuple[int, int]: Width and height in pixels
        """
        if hw_type not in self._tag_types:
            return 296, 128  # Safe defaults
        return self._tag_types[hw_type].width, self._tag_types[hw_type].height

    def get_hw_string(self, hw_type: int) -> str:
        """Get the display name for a hardware type.

        Returns a human-readable name for the tag hardware type.

        Args:
            hw_type: Hardware type ID number

        Returns:
            str: Human-readable name or "Unknown Type {hw_type}" if not recognized
        """
        if hw_type not in self._tag_types:
            return f"Unknown Type {hw_type}"
        return self._tag_types[hw_type].get('name', f'Unknown Type {hw_type}')

    def is_in_hw_map(self, hw_type: int) -> bool:
        """Check if a hardware type is known to the manager.

        Determines whether the specified hardware type ID has a
        definition available in the manager.

        Args:
            hw_type: Hardware type ID to check

        Returns:
            bool: True if the hardware type is known, False otherwise
        """
        return hw_type in self._tag_types

    def get_all_types(self) -> Dict[int, TagType]:
        """Return all known tag types.

        Provides a copy of the complete type map.
        This is useful for debugging or for UIs that
        need to display all available tag types.

        Returns:
            Dict[int, TagType]: Dictionary mapping type IDs to TagType instances
        """
        return self._tag_types.copy()


# Update the helper functions to be synchronous after initial load
_INSTANCE: Optional[TagTypesManager] = None


async def get_tag_types_manager(hass: HomeAssistant) -> TagTypesManager:
    """Get or create the global TagTypesManager instance.

    Implements a singleton pattern to ensure only one tag types manager
    exists per Home Assistant instance. If the manager doesn't exist yet,
    creates and initializes it.

    Args:
        hass: Home Assistant instance

    Returns:
        TagTypesManager: The shared manager instance
    """
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = TagTypesManager(hass)
        await _INSTANCE.ensure_types_loaded()
    return _INSTANCE


def reset_tag_types_manager() -> None:
    """Reset the global TagTypesManager instance.
    
    Called when the integration storage files are being removed
    to ensure the singleton gets recreated on next access.
    """
    global _INSTANCE
    _INSTANCE = None


def get_hw_dimensions(hw_type: int) -> Tuple[int, int]:
    """Get dimensions synchronously from global instance.

    Synchronous wrapper around the TagTypesManager.get_hw_dimensions method
    that uses the global manager instance. Returns default dimensions
    if the manager isn't initialized yet.

    Args:
        hw_type: Hardware type ID number

    Returns:
        Tuple[int, int]: Width and height in pixels (defaults to 296x128)
    """
    if _INSTANCE is None:
        return 296, 128  # Default dimensions
    return _INSTANCE.get_hw_dimensions(hw_type)


def get_hw_string(hw_type: int) -> str:
    """Get display name synchronously from global instance.

    Synchronous wrapper around the TagTypesManager.get_hw_string method
    that uses the global manager instance. Returns a default string
    if the manager isn't initialized yet.

    Args:
        hw_type: Hardware type ID number

    Returns:
        str: Human-readable name or "Unknown Type {hw_type}" if not recognized
    """
    if _INSTANCE is None:
        return f"Unknown Type {hw_type}"
    return _INSTANCE.get_hw_string(hw_type)


def is_in_hw_map(hw_type: int) -> bool:
    """Get display name synchronously from global instance.

    Synchronous wrapper around the TagTypesManager.is_in_hw_map method
    that uses the global manager instance. Returns `false`
    if the manager isn't initialized yet.

    Args:
        hw_type: Hardware type ID number

    Returns:
        bool: True if the hardware type is known, False otherwise
    """
    if _INSTANCE is None:
        return False
    return _INSTANCE.is_in_hw_map(hw_type)
