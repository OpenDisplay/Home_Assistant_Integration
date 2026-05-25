"""Shared BLE image upload protocol (compatible with both ATC and OpenDisplay firmware)."""
import asyncio
import struct
import zlib
import logging
from enum import Enum
from time import perf_counter

import numpy as np
from PIL import Image

from .exceptions import BLEError
from .image_processing import process_image_for_device
from ..metadata import BLEDeviceMetadata

_LOGGER = logging.getLogger(__name__)


# BLE Protocol Sizes
BLE_BLOCK_SIZE = 4096
BLE_MAX_PACKET_DATA_SIZE = 230
DIRECT_WRITE_COMPRESSED_BUFFER_LIMIT = 50 * 1024
DIRECT_WRITE_COMPRESSION_CHUNK_BYTES = 64 * 1024


class BLEResponse(Enum):
    """BLE upload response codes."""

    BLOCK_REQUEST = "00C6"
    BLOCK_PART_ACK = "00C4"
    BLOCK_PART_CONTINUE = "00C5"
    UPLOAD_COMPLETE = "00C7"
    IMAGE_ALREADY_DISPLAYED = "00C8"
    # Direct write responses
    DIRECT_WRITE_START_ACK = "0070"
    DIRECT_WRITE_START_ACK_ALT = "7000"  # Alternative format
    DIRECT_WRITE_DATA_ACK = "0071"
    DIRECT_WRITE_DATA_ACK_ALT = "7100"  # Alternative format
    DIRECT_WRITE_END_ACK = "0072"
    DIRECT_WRITE_END_ACK_ALT = "7200"  # Alternative format


class BLECommand(Enum):
    """BLE upload command codes."""

    DATA_INFO = "0064"
    BLOCK_PART = "0065"
    # Direct write commands
    DIRECT_WRITE_START = "0070"
    DIRECT_WRITE_DATA = "0071"
    DIRECT_WRITE_END = "0072"


class BLEDataType(Enum):
    """BLE image data types."""

    RAW_BW = 0x20  # Uncompressed monochrome
    RAW_COLOR = 0x21  # Uncompressed color (BWR/BWY)
    COMPRESSED = 0x30  # Compressed image


class RefreshMode(Enum):
    """Epaper display refresh modes."""
    FULL = 0
    FAST = 1
    PARTIAL = 2
    PARTIAL2 = 3


def _create_data_info(
    checksum: int,
    data_ver: int,
    data_size: int,
    data_type: int,
    data_type_argument: int,
    next_check_in: int,
) -> bytes:
    """Create data info packet for image upload.

    Args:
        checksum: Data checksum (usually 255 placeholder)
        data_ver: CRC32 of image data
        data_size: Image data size in bytes
        data_type: Data type enum value (0x20, 0x21, 0x30)
        data_type_argument: Additional argument (usually 0)
        next_check_in: Next check-in time (usually 0)

    Returns:
        bytes: Packed data info structure
    """
    return struct.pack(
        "<BQIBBH",
        checksum,
        data_ver,
        data_size,
        data_type,
        data_type_argument,
        next_check_in,
    )


def _create_block_part(block_id: int, part_id: int, data: bytes) -> bytearray:
    """Create a block part packet for image upload.

    Args:
        block_id: Block identifier
        part_id: Part identifier within block
        data: Packet data (max 230 bytes)

    Returns:
        bytearray: Block part packet with checksum

    Raises:
        ValueError: If data exceeds maximum size
    """
    max_data_size = 230
    data_length = len(data)
    if data_length > max_data_size:
        raise ValueError("Data length exceeds maximum allowed size for a packet.")

    buffer = bytearray(3 + max_data_size)
    buffer[1] = block_id & 0xFF
    buffer[2] = part_id & 0xFF
    buffer[3 : 3 + data_length] = data
    buffer[0] = sum(buffer[1 : 3 + data_length]) & 0xFF
    return buffer


def _convert_image_to_bytes(
    image: Image.Image,
    color_scheme: int = 0,
    compressed: bool = False
) -> tuple[int, bytes]:
    """
    Convert a PIL Image to device format.

    Expects image to be pre-quantized to exact palette colors (via dithering).
    Uses exact color matching instead of luminance-based detection.

    Supports:
    - Monochrome (1-bit)
    - Color dual-plane (BWR/BWY/BWRY)
    - Optional zlib compression

    Args:
        image: PIL Image to convert (should be pre-quantized)
        color_scheme: Color scheme int (0=mono, 1=BWR, 2=BWY, 3=BWRY)
        compressed: Whether to compress the data

    Returns:
        tuple: (data_type, pixel_array)
      """
    pixel_array = np.array(image.convert("RGB"))
    height, width, _ = pixel_array.shape

    # Get RGB channels
    r = pixel_array[:, :, 0]
    g = pixel_array[:, :, 1]
    b = pixel_array[:, :, 2]

    # Exact color matching (image already quantized by dithering)
    black_pixels = (r == 0) & (g == 0) & (b == 0)
    # white_pixels = (r == 255) & (g == 255) & (b == 255)
    red_pixels = (r == 255) & (g == 0) & (b == 0)
    yellow_pixels = (r == 255) & (g == 255) & (b == 0)

    # Determine if multi-color mode
    multi_color = color_scheme in (1, 2, 3)  # BWR, BWY, or BWRY

    # Dual-plane encoding:
    # Plane 1 (BW): 1 = black or yellow, 0 = white or red
    # Plane 2 (color): 1 = red or yellow, 0 = black or white
    bw_channel_bits = black_pixels | yellow_pixels

    byte_data = np.packbits(bw_channel_bits).tobytes()
    bpp_array = bytearray(byte_data)

    if multi_color:
        color_pixels = red_pixels | yellow_pixels
        byte_data_color = np.packbits(color_pixels).tobytes()
        bpp_array += byte_data_color

    if compressed:
        buffer = bytearray(6)
        buffer[0] = 6
        buffer[1] = width & 0xFF
        buffer[2] = (width >> 8) & 0xFF
        buffer[3] = height & 0xFF
        buffer[4] = (height >> 8) & 0xFF
        buffer[5] = 0x02 if multi_color else 0x01
        buffer += bpp_array
        the_compressor = zlib.compressobj(wbits=12)
        compressed_data = the_compressor.compress(buffer)
        compressed_data += the_compressor.flush()
        return (
            BLEDataType.COMPRESSED.value,
            struct.pack("<I", len(buffer)) + compressed_data,
        )

    return (
        BLEDataType.RAW_COLOR.value if multi_color else BLEDataType.RAW_BW.value,
        bpp_array,
    )


def _detect_color(r: int, g: int, b: int, color_scheme: int) -> str:
    """Detect color from RGB values based on color scheme.
    
    Args:
        r: Red component (0-255)
        g: Green component (0-255)
        b: Blue component (0-255)
        color_scheme: Color scheme identifier
        
    Returns:
        Color name: 'black', 'white', 'red', 'yellow', 'green', 'blue'
    """
    if r < 128 and g < 128 and b < 128:
        return 'black'
    if r > 200 and g > 200 and b > 200:
        return 'white'
    
    if color_scheme == 0:
        return 'white' if (r + g + b) / 3 > 128 else 'black'
    
    if color_scheme in (1, 3, 4):
        if r > 200 and g < 100 and b < 100:
            return 'red'
    
    if color_scheme in (2, 3, 4):
        if r > 200 and g > 200 and b < 100:
            return 'yellow'
    
    if color_scheme == 4:
        if r < 100 and g > 200 and b < 100:
            return 'green'
        if r < 100 and g < 100 and b > 200:
            return 'blue'
    
    return 'white' if (r + g + b) / 3 > 128 else 'black'


def _direct_write_color_values(pixel_array: np.ndarray, color_scheme: int) -> np.ndarray:
    """Return direct-write firmware color values using the same thresholds as _detect_color."""
    flat = pixel_array.reshape(-1, 3)
    r = flat[:, 0]
    g = flat[:, 1]
    b = flat[:, 2]
    average = (r.astype(np.uint16) + g.astype(np.uint16) + b.astype(np.uint16)) / 3.0

    values = np.where(average > 128, 1, 0).astype(np.uint8)
    values[(r < 128) & (g < 128) & (b < 128)] = 0
    values[(r > 200) & (g > 200) & (b > 200)] = 1

    if color_scheme in (1, 3, 4):
        values[(r > 200) & (g < 100) & (b < 100)] = 3

    if color_scheme in (2, 3, 4):
        values[(r > 200) & (g > 200) & (b < 100)] = 2

    if color_scheme == 4:
        values[(r < 100) & (g > 200) & (b < 100)] = 6
        values[(r < 100) & (g < 100) & (b > 200)] = 5

    return values


def _encode_direct_write_1bpp(image: Image.Image) -> bytes:
    """Encode image as 1BPP for direct write (monochrome).
    
    Args:
        image: PIL Image to encode
        
    Returns:
        bytes: 1BPP encoded data (white=1, black=0, NOT inverted)
    """
    pixel_array = np.asarray(image.convert("RGB"), dtype=np.uint8).reshape(-1, 3)
    gray = (
        pixel_array[:, 0].astype(np.uint16)
        + pixel_array[:, 1].astype(np.uint16)
        + pixel_array[:, 2].astype(np.uint16)
    ) / 3.0
    return np.packbits(gray > 128).tobytes()


def _encode_direct_write_bitplanes(image: Image.Image, color_scheme: int) -> bytes:
    """Encode image as bitplanes for direct write (BWR/BWY).
    
    Args:
        image: PIL Image to encode
        color_scheme: Color scheme (1=BWR, 2=BWY)
        
    Returns:
        bytes: Plane 1 (B/W, NOT inverted) + Plane 2 (R/Y)
    """
    pixel_array = np.asarray(image.convert("RGB"), dtype=np.uint8)
    colors = _direct_write_color_values(pixel_array, color_scheme)
    # Plane 1 is B/W: 1=white/red, 0=black/yellow. Plane 2 is color: 1=red/yellow.
    plane1 = (colors == 1) | (colors == 3)
    plane2 = (colors == 2) | (colors == 3)
    return np.packbits(plane1).tobytes() + np.packbits(plane2).tobytes()


def _encode_direct_write_2bpp(image: Image.Image, color_scheme: int) -> bytes:
    """Encode image as 2BPP for direct write (BWRY or 4 grayscale).
    
    Args:
        image: PIL Image to encode
        color_scheme: Color scheme (3=BWRY, 5=4 grayscale)
        
    Returns:
        bytes: 2BPP encoded data (4 pixels per byte)
    """
    pixel_array = np.asarray(image.convert("RGB"), dtype=np.uint8)

    if color_scheme == 5:
        flat = pixel_array.reshape(-1, 3)
        gray = (
            flat[:, 0].astype(np.uint16)
            + flat[:, 1].astype(np.uint16)
            + flat[:, 2].astype(np.uint16)
        ) / 3.0
        # 4 grayscale: 00=black, 01=dark gray, 10=light gray, 11=white.
        values = np.where(gray < 64, 0, np.where(gray < 128, 1, np.where(gray < 192, 2, 3))).astype(np.uint8)
    else:
        # BWRY: 00=black, 01=white, 10=yellow, 11=red.
        colors = _direct_write_color_values(pixel_array, color_scheme)
        values = np.zeros_like(colors)
        values[colors == 1] = 1
        values[colors == 2] = 2
        values[colors == 3] = 3

    pad = (-len(values)) % 4
    if pad:
        values = np.pad(values, (0, pad))
    groups = values.reshape(-1, 4)
    # Pack from MSB: pixel0 at bits 7-6, pixel1 at 5-4, pixel2 at 3-2, pixel3 at 1-0.
    return (
        (groups[:, 0] << 6)
        | (groups[:, 1] << 4)
        | (groups[:, 2] << 2)
        | groups[:, 3]
    ).astype(np.uint8).tobytes()


def _encode_direct_write_4bpp(image: Image.Image) -> bytes:
    """Encode image as 4BPP for direct write (6-color).
    
    Args:
        image: PIL Image to encode
        
    Returns:
        bytes: 4BPP encoded data (2 pixels per byte)
    """
    pixel_array = np.asarray(image.convert("RGB"), dtype=np.uint8)
    # Firmware expects: black=0, white=1, yellow=2, red=3, blue=5, green=6.
    values = _direct_write_color_values(pixel_array, 4)
    pad = (-len(values)) % 2
    if pad:
        values = np.pad(values, (0, pad))
    pairs = values.reshape(-1, 2)
    # Pack two pixels per byte: first pixel in the high nibble, second in the low nibble.
    return ((pairs[:, 0] << 4) | pairs[:, 1]).astype(np.uint8).tobytes()


def _encode_direct_write(image: Image.Image, color_scheme: int) -> bytes:
    """Encode image for direct write based on color scheme.
    
    Args:
        image: PIL Image to encode
        color_scheme: Color scheme (0=b/w, 1=bwr, 2=bwy, 3=bwry, 4=bwgbry, 5=bw4)
        
    Returns:
        bytes: Encoded image data
    """
    if color_scheme == 0:
        return _encode_direct_write_1bpp(image)
    elif color_scheme in (1, 2):
        return _encode_direct_write_bitplanes(image, color_scheme)
    elif color_scheme == 3:
        return _encode_direct_write_2bpp(image, color_scheme)
    elif color_scheme == 4:
        return _encode_direct_write_4bpp(image)
    elif color_scheme == 5:
        return _encode_direct_write_2bpp(image, color_scheme)
    else:
        # Fallback to 1BPP
        return _encode_direct_write_1bpp(image)


def _prepare_block_upload(
    image: Image.Image,
    metadata: BLEDeviceMetadata,
    protocol_type: str,
    dither: int,
) -> tuple[Image.Image, int, bytes, float]:
    """Prepare block-based BLE upload bytes off the event loop."""
    # ATC stores rotated image memory client-side; OpenDisplay handles rotation firmware-side.
    if protocol_type == "atc" and metadata.rotatebuffer == 1:
        image = image.transpose(Image.Transpose.ROTATE_90)
        _LOGGER.debug("Applied 90° ATC memory rotation: %dx%d", image.width, image.height)

    quantize_start = perf_counter()
    processed_image = process_image_for_device(
        image,
        metadata.color_scheme.value,
        dither,
    )
    quantize_duration = perf_counter() - quantize_start
    data_type, pixel_array = _convert_image_to_bytes(
        processed_image,
        metadata.color_scheme.value,
        compressed=True,
    )
    return processed_image, data_type, pixel_array, quantize_duration


def _prepare_direct_write_upload(
    image: Image.Image,
    metadata: BLEDeviceMetadata,
    allow_compression: bool,
    dither: int,
) -> tuple[Image.Image, bytes, list[bytes], int, int, bool, float]:
    """Prepare direct-write BLE upload bytes off the event loop."""
    quantize_start = perf_counter()
    processed_image = process_image_for_device(
        image,
        metadata.color_scheme.value,
        dither,
    )
    quantize_duration = perf_counter() - quantize_start
    encoded_data = _encode_direct_write(processed_image, metadata.color_scheme.value)
    compressed_data = (
        _compress_direct_write_if_fits(encoded_data, DIRECT_WRITE_COMPRESSED_BUFFER_LIMIT)
        if allow_compression
        else None
    )

    if compressed_data is not None:
        data_to_send = compressed_data
        uncompressed_size = len(encoded_data)
        compressed = True
    else:
        data_to_send = encoded_data
        uncompressed_size = 0
        compressed = False

    chunks = [
        data_to_send[i:i + BLE_MAX_PACKET_DATA_SIZE]
        for i in range(0, len(data_to_send), BLE_MAX_PACKET_DATA_SIZE)
    ]
    return processed_image, data_to_send, chunks, uncompressed_size, len(encoded_data), compressed, quantize_duration


def _compress_direct_write_if_fits(data: bytes, max_size: int) -> bytes | None:
    """Compress direct-write data, returning None once the compressed payload is too large."""
    compressor = zlib.compressobj(level=9)
    data_view = memoryview(data)
    compressed_parts = []
    compressed_size = 0

    for start in range(0, len(data_view), DIRECT_WRITE_COMPRESSION_CHUNK_BYTES):
        part = compressor.compress(
            data_view[start:start + DIRECT_WRITE_COMPRESSION_CHUNK_BYTES]
        )
        if part:
            compressed_parts.append(part)
            compressed_size += len(part)
            if compressed_size >= max_size:
                return None

    part = compressor.flush()
    if part:
        compressed_parts.append(part)
        compressed_size += len(part)
        if compressed_size >= max_size:
            return None

    return b"".join(compressed_parts)


class BLEImageUploader:
    """Handles BLE image upload with block-based protocol.

    This class is protocol-agnostic and works with both ATC and OpenDisplay firmware.
    """

    def __init__(self, connection, mac_address: str):
        """Initialize image uploader.

        Args:
            connection: Active BLEConnection instance
            mac_address: Device MAC address
        """
        self.connection = connection
        self.mac_address = mac_address
        self._img_array = b""
        self._img_array_len = 0
        self._packets = []
        self._packet_index = 0
        self._upload_complete = asyncio.Event()
        self._upload_error = None
        self._upload_task = None
        # Direct write state
        self._direct_write_chunks = []
        self._direct_write_chunk_index = 0
        self._direct_write_pending_acks = 0
        self._direct_write_compressed = False
        self._direct_write_uncompressed_size = 0
        self.refresh_type: int = 0

    async def _handle_response(self, data: bytes) -> bool:
        """Handle upload responses from notification queue.

        Args:
            data: Response data from device

        Returns:
            bool: True if response was handled successfully
        """
        if len(data) < 2:
            return False

        response_code = data[:2].hex().upper()
        _LOGGER.debug("Upload response for %s: %s", self.mac_address, response_code)

        try:
            response_enum = BLEResponse(response_code)
            match response_enum:
                case BLEResponse.BLOCK_REQUEST:
                    _LOGGER.debug("Received block request")
                    block_id = data[11]
                    if len(data) >= 18:
                        requested_parts_hex = data[12:18].hex().upper()
                        _LOGGER.debug(
                            "Device requested block %d, parts bitmask: %s",
                            block_id,
                            requested_parts_hex,
                        )
                    else:
                        _LOGGER.debug(
                            "Device requested block %d (partial block request data)", block_id
                        )
                    await self._send_block_data(block_id)
                    return True

                case BLEResponse.BLOCK_PART_ACK:
                    _LOGGER.debug("Block part acknowledged")
                    await self._send_next_block_part()
                    return True

                case BLEResponse.BLOCK_PART_CONTINUE:
                    _LOGGER.debug("Block part acknowledged, continuing")
                    if self._packet_index >= len(self._packets):
                        _LOGGER.error("Packet index out of range")
                        return True
                    self._packet_index += 1
                    await self._send_next_block_part()
                    return True

                case BLEResponse.UPLOAD_COMPLETE:
                    _LOGGER.debug("Image upload completed successfully")
                    self._upload_complete.set()
                    return True

                case BLEResponse.IMAGE_ALREADY_DISPLAYED:
                    _LOGGER.debug("Image already displayed")
                    self._upload_complete.set()
                    return True

        except ValueError:
            return False  # Unknown response code

    async def upload_image_block_based(
            self,
            image: Image.Image,
            metadata: BLEDeviceMetadata,
            protocol_type: str = "atc",
            dither: int = 2,
            render_duration: float | None = None,
    ) -> tuple[bool, Image.Image | None]:
        """Upload image using block-based protocol.

        Args:
            image: Rendered image
            metadata: Device metadata with dimensions and color support
            protocol_type: Protocol type ("atc" or "open_display")
            dither: 0=none, 1=ordered, 2=floyd-steinberg
            render_duration: Time spent rendering the image before upload, in seconds

        Returns:
            tuple: (success, processed_image) - processed_image is the dithered PIL Image
        """
        try:
            _LOGGER.debug(
                "Block upload input for %s: %dx%d (protocol=%s, rotatebuffer=%d)",
                self.mac_address,
                image.width,
                image.height,
                protocol_type,
                metadata.rotatebuffer,
            )

            processed_image, data_type, pixel_array, quantize_duration = await self.connection.hass.async_add_executor_job(
                _prepare_block_upload,
                image,
                metadata,
                protocol_type,
                dither,
            )

            _LOGGER.debug(
                "Upload for %s: DataType=0x%02x, DataLen=%d",
                self.mac_address,
                data_type,
                len(pixel_array),
            )
            _LOGGER.info(
                "Starting BLE image upload to %s (%d bytes)", self.mac_address, len(pixel_array)
            )

            self._img_array = pixel_array
            self._img_array_len = len(self._img_array)

            # Send data info to initiate upload
            data_info = _create_data_info(
                255, zlib.crc32(self._img_array) & 0xFFFFFFF, self._img_array_len, data_type, 0, 0
            )
            send_refresh_start = perf_counter()
            await self.connection._write_raw(bytes.fromhex(BLECommand.DATA_INFO.value) + data_info)

            # Wait for responses using request-response pattern
            while not self._upload_complete.is_set():
                response = await self._wait_for_response()
                if response and await self._handle_response(response):
                    continue
                elif response is None:
                    # Timeout - this is a failure
                    _LOGGER.error("Upload failed for %s: timeout waiting for response", self.mac_address)
                    return False, None

            if self._upload_error:
                raise BLEError(f"Upload failed: {self._upload_error}")

            # Only reach here if upload_complete was set by a success response
            send_refresh_duration = perf_counter() - send_refresh_start
            _LOGGER.info(
                "BLE block upload completed for %s: render=%.3fs dither_quantize=%.3fs send_refresh=%.3fs bytes=%d data_type=0x%02x",
                self.mac_address,
                render_duration or 0.0,
                quantize_duration,
                send_refresh_duration,
                len(pixel_array),
                data_type,
            )
            return True, processed_image

        except Exception as e:
            _LOGGER.error("Image upload failed for %s: %s", self.mac_address, e)
            return False, None

    async def _wait_for_response(self, timeout: float = 10.0) -> bytes | None:
        """Wait for next upload response with timeout.

        Args:
            timeout: Timeout in seconds

        Returns:
            bytes: Response data or None if timeout
        """
        try:
            response = await asyncio.wait_for(
                self.connection._response_queue.get(), timeout=timeout
            )

            # Basic validation only
            if not response or len(response) < 2:
                return None

            return response

        except asyncio.TimeoutError:
            return None

    async def _send_block_data(self, block_id: int):
        """Send block data for specified block ID.

        Args:
            block_id: Block identifier to send
        """
        _LOGGER.debug("Building block %d for %s", block_id, self.mac_address)
        block_start = block_id * BLE_BLOCK_SIZE
        block_end = block_start + BLE_BLOCK_SIZE
        block_data = self._img_array[block_start:block_end]

        _LOGGER.debug(
            "Sending block %d: %d bytes (offset %d-%d)",
            block_id,
            len(block_data),
            block_start,
            min(block_end, len(self._img_array)),
        )

        crc_block = sum(block_data) & 0xFFFF
        buffer = bytearray(4)
        buffer[0] = len(block_data) & 0xFF
        buffer[1] = (len(block_data) >> 8) & 0xFF
        buffer[2] = crc_block & 0xFF
        buffer[3] = (crc_block >> 8) & 0xFF
        block_data = buffer + block_data

        # Create packets
        packet_count = (len(block_data) + BLE_MAX_PACKET_DATA_SIZE - 1) // BLE_MAX_PACKET_DATA_SIZE
        self._packets = []
        for i in range(packet_count):
            start = i * BLE_MAX_PACKET_DATA_SIZE
            end = start + BLE_MAX_PACKET_DATA_SIZE
            slice_data = block_data[start:end]
            packet = _create_block_part(block_id, i, slice_data)
            self._packets.append(packet)

        _LOGGER.debug("Created %d packets for block %d", len(self._packets), block_id)
        self._packet_index = 0
        if self._packets:
            await self._send_next_block_part()

    async def _send_next_block_part(self):
        """Send next block part packet."""
        if not self._packets or self._packet_index >= len(self._packets):
            _LOGGER.debug("No more packets to send")
            return

        _LOGGER.debug("Sending packet %d/%d", self._packet_index + 1, len(self._packets))
        await self.connection._write_raw(
            bytes.fromhex(BLECommand.BLOCK_PART.value) + self._packets[self._packet_index]
        )

    async def upload_direct_write(
        self,
        image: Image.Image,
        metadata: BLEDeviceMetadata,
        allow_compression: bool = False,
        dither: int = 2,
        refresh_type: int = 0,
        render_duration: float | None = None,
    ) -> tuple[bool, Image.Image | None]:
        """Upload image using direct write protocol (OpenDisplay only).
        
        Args:
            image: Rendered image
            metadata: Device metadata with dimensions and color scheme
            allow_compression: Whether zip compression may be used if the result fits
            dither: 0=none, 1=ordered, 2=floyd-steinberg
            refresh_type: Display refresh mode (0=full, 1=fast, 2=partial, 3=partial2)
            render_duration: Time spent rendering the image before upload, in seconds
            
        Returns:
            bool: True if upload succeeded, False otherwise
        """
        # Reset upload state
        self._upload_complete.clear()
        self._upload_error = None

        self.refresh_type = refresh_type
        
        try:
            _LOGGER.debug("Direct write: image size %dx%d", image.width, image.height)

            (
                processed_image,
                data_to_send,
                chunks,
                uncompressed_size,
                encoded_size,
                compressed,
                quantize_duration,
            ) = await self.connection.hass.async_add_executor_job(
                _prepare_direct_write_upload,
                image,
                metadata,
                allow_compression,
                dither,
            )

            if compressed:
                _LOGGER.debug(
                    "Direct write compressed: %d bytes -> %d bytes",
                    encoded_size,
                    len(data_to_send)
                )
            elif allow_compression:
                _LOGGER.debug(
                    "Direct write compression skipped: compressed payload exceeded %d bytes",
                    DIRECT_WRITE_COMPRESSED_BUFFER_LIMIT,
                )
            
            _LOGGER.info(
                "Starting direct write upload to %s (%d bytes%s, refresh type %d)",
                self.mac_address,
                len(data_to_send),
                " compressed" if compressed else "",
                refresh_type
            )
            
            # Initialize direct write state
            self._direct_write_chunks = []
            self._direct_write_chunk_index = 0
            self._direct_write_pending_acks = 0
            self._direct_write_compressed = compressed
            self._direct_write_uncompressed_size = uncompressed_size
            self._direct_write_chunks = chunks
            
            _LOGGER.debug("Split into %d chunks", len(self._direct_write_chunks))
            
            # Send start command
            send_refresh_start = perf_counter()
            if compressed:
                # Compressed: send 4-byte header + initial data if it fits
                header = struct.pack("<I", uncompressed_size)
                max_start_payload = 200  # Leave room for command bytes
                
                if len(header) + len(data_to_send) <= max_start_payload:
                    # Small payload - send everything in start command
                    start_payload = header + data_to_send
                    await self.connection._write_raw(
                        bytes.fromhex(BLECommand.DIRECT_WRITE_START.value) + start_payload
                    )
                    # Mark all chunks as sent
                    self._direct_write_chunk_index = len(self._direct_write_chunks)
                else:
                    # Large payload - send header + first chunk
                    first_chunk_size = min(max_start_payload - len(header), BLE_MAX_PACKET_DATA_SIZE)
                    first_chunk = data_to_send[:first_chunk_size]
                    start_payload = header + first_chunk
                    await self.connection._write_raw(
                        bytes.fromhex(BLECommand.DIRECT_WRITE_START.value) + start_payload
                    )
                    # Adjust chunks - remove first chunk data that was sent
                    if self._direct_write_chunks and len(self._direct_write_chunks[0]) <= first_chunk_size:
                        self._direct_write_chunks.pop(0)
                    else:
                        self._direct_write_chunks[0] = self._direct_write_chunks[0][first_chunk_size:]
            else:
                # Uncompressed: just send start command
                await self.connection._write_raw(
                    bytes.fromhex(BLECommand.DIRECT_WRITE_START.value)
                )
            
            # Wait for responses
            while not self._upload_complete.is_set():
                response = await self._wait_for_response(timeout=30.0)
                if response and await self._handle_direct_write_response(response):
                    continue
                elif response is None:
                    _LOGGER.error("Direct write failed for %s: timeout", self.mac_address)
                    return False, None
            
            if self._upload_error:
                raise BLEError(f"Direct write failed: {self._upload_error}")
            
            send_refresh_duration = perf_counter() - send_refresh_start
            _LOGGER.info(
                "Direct write upload completed for %s: render=%.3fs dither_quantize=%.3fs send_refresh=%.3fs bytes=%d encoded_bytes=%d compressed=%s refresh_type=%d",
                self.mac_address,
                render_duration or 0.0,
                quantize_duration,
                send_refresh_duration,
                len(data_to_send),
                encoded_size,
                compressed,
                refresh_type,
            )
            return True, processed_image
            
        except Exception as e:
            _LOGGER.error("Direct write upload failed for %s: %s", self.mac_address, e)
            return False, None

    async def _handle_direct_write_response(self, data: bytes) -> bool:
        """Handle direct write responses.
        
        Args:
            data: Response data from device
            
        Returns:
            bool: True if response was handled successfully
        """
        if len(data) < 2:
            return False
        
        response_code = data[:2].hex().upper()
        _LOGGER.debug("Direct write response for %s: %s", self.mac_address, response_code)
        
        try:
            # Handle both formats: "0070" and "7000"
            if response_code in ("0070", "7000"):
                # Start ACK
                _LOGGER.debug("Direct write start acknowledged")
                self._direct_write_pending_acks = 0
                await self._send_next_direct_write_chunks()
                return True
            elif response_code in ("0071", "7100"):
                # Data ACK
                self._direct_write_pending_acks = max(0, self._direct_write_pending_acks - 1)
                await self._send_next_direct_write_chunks()
                return True
            elif response_code in ("0072", "7200"):
                # End ACK
                _LOGGER.debug("Direct write end acknowledged")
                self._upload_complete.set()
                return True
            elif response_code == "FFFF":
                # Error
                _LOGGER.error("Direct write error response (FFFF)")
                self._upload_error = "Device returned error (FFFF)"
                self._upload_complete.set()
                return True
        except Exception as e:
            _LOGGER.error("Error handling direct write response: %s", e)
            return False
        
        return False  # Unknown response code

    async def _send_next_direct_write_chunks(self):
        """Send next direct write data chunks with pipelining."""
        DIRECT_WRITE_PIPELINE_SIZE = 1  # Send up to 1 chunk without waiting for ACK
        
        while (self._direct_write_chunk_index < len(self._direct_write_chunks) and
               self._direct_write_pending_acks < DIRECT_WRITE_PIPELINE_SIZE):
            
            chunk = self._direct_write_chunks[self._direct_write_chunk_index]
            _LOGGER.debug(
                "Sending direct write chunk %d/%d (%d bytes)",
                self._direct_write_chunk_index + 1,
                len(self._direct_write_chunks),
                len(chunk)
            )
            
            await self.connection._write_raw(
                bytes.fromhex(BLECommand.DIRECT_WRITE_DATA.value) + chunk
            )
            
            self._direct_write_chunk_index += 1
            self._direct_write_pending_acks += 1
        
        # If all chunks sent and no pending ACKs, send end command
        if (self._direct_write_chunk_index >= len(self._direct_write_chunks) and
            self._direct_write_pending_acks == 0):
            _LOGGER.debug("All chunks sent, ending direct write")
            await self.connection._write_raw(
                bytes.fromhex(BLECommand.DIRECT_WRITE_END.value) + bytes([self.refresh_type])
            )
