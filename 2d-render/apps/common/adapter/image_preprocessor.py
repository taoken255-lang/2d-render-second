"""Universal image preprocessing for video generation adapters.

Flexible image validation, resizing, format conversion, and encoding with configurable
constraints. Adapter-agnostic - each adapter defines its own configuration.

Usage:
    # Agnet adapter: max dimension + JPG conversion
    agnet_config = ImagePreprocessorConfig(
        max_dimension=1920,
        convert_to_jpg=True,
        jpg_quality=90,
        max_file_size_mb=10,
        ensure_even=True,
    )
    preprocessor = ImagePreprocessor(agnet_config)
    img_bytes, w, h = preprocessor.preprocess(Path("photo.png"))

    # Finik adapter: preset resolution with pad/crop
    finik_config = ImagePreprocessorConfig(
        preset="480p",        # or "720p"
        resize_mode="pad",    # or "crop", "fit"
        ensure_even=True,
    )
    preprocessor = ImagePreprocessor(finik_config)
    img_bytes, w, h = preprocessor.preprocess(Path("photo.png"))

References:
- /home/igor/repos/2d-render/docs/input.md (Agnet constraints)
- /home/igor/repos/infinitetalk/apps/common/image_preprocessor.py (Finik presets)
"""

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image


@dataclass
class ImagePreprocessorConfig:
    """Configuration for image preprocessing.

    Pure data container - no adapter-specific knowledge.
    Each adapter defines its own configuration instance.

    Attributes:
        min_dimension: Minimum allowed width or height (optional)
        max_dimension: Maximum allowed width or height (optional)
        target_resolution: Specific target (width, height) tuple (optional)
        preset: Preset name ("480p", "720p") for fixed resolutions (optional)
        resize_mode: Resize mode - "fit" (maintain aspect), "pad" (add borders), "crop" (cut edges)
        convert_to_jpg: Whether to convert images to JPG format (default: False)
        jpg_quality: JPG compression quality 0-100 (default: 90)
        auto_resize: Whether to auto-resize oversized images (default: True)
        max_file_size_mb: Maximum file size in MB (optional)
        ensure_even: Ensure dimensions are multiples of 2 (default: False)
    """

    # Resize constraints (mutually exclusive groups)
    min_dimension: Optional[int] = None
    max_dimension: Optional[int] = None
    target_resolution: Optional[Tuple[int, int]] = None

    # Preset support (for adapters that need fixed resolutions)
    preset: Optional[str] = None  # e.g., "480p", "720p"
    resize_mode: str = "fit"  # "fit", "pad", "crop"

    # Format conversion
    convert_to_jpg: bool = False
    jpg_quality: int = 90
    auto_resize: bool = True

    # Validation
    max_file_size_mb: Optional[int] = None
    ensure_even: bool = False

    def __post_init__(self):
        """Validate configuration parameters."""
        if self.min_dimension is not None and self.min_dimension <= 0:
            raise ValueError(f"min_dimension must be positive, got {self.min_dimension}")

        if self.max_dimension is not None and self.max_dimension <= 0:
            raise ValueError(f"max_dimension must be positive, got {self.max_dimension}")

        if self.min_dimension and self.max_dimension and self.min_dimension > self.max_dimension:
            raise ValueError(
                f"min_dimension ({self.min_dimension}) cannot exceed max_dimension ({self.max_dimension})"
            )

        if not 0 <= self.jpg_quality <= 100:
            raise ValueError(f"jpg_quality must be 0-100, got {self.jpg_quality}")

        if self.max_file_size_mb is not None and self.max_file_size_mb <= 0:
            raise ValueError(f"max_file_size_mb must be positive, got {self.max_file_size_mb}")


class ImagePreprocessor:
    """Universal image preprocessor for video generation adapters.

    Validates and transforms input images with configurable constraints:
    - Resizes images based on min/max dimension, target resolution, or preset
    - Resize modes: fit (maintain aspect), pad (add borders), crop (cut edges)
    - Converts to JPG format (optional)
    - Ensures dimensions are multiples of 2 (for video codecs)
    - Validates file size limits

    Usage:
        # Define config in adapter module
        config = ImagePreprocessorConfig(
            max_dimension=1920,
            convert_to_jpg=True,
            ensure_even=True,
        )
        preprocessor = ImagePreprocessor(config)
        img_bytes, w, h = preprocessor.preprocess(Path("photo.png"))
    """

    # Finik resolution presets for portrait/landscape/square orientations
    PRESETS = {
        "480p": {
            "portrait": (480, 832),
            "landscape": (832, 480),
            "square": (640, 640),
        },
        "720p": {
            "portrait": (720, 1280),
            "landscape": (1280, 720),
            "square": (960, 960),
        },
    }

    def __init__(self, config: ImagePreprocessorConfig):
        """Initialize preprocessor with configuration.

        Args:
            config: ImagePreprocessorConfig instance with constraints
        """
        self.config = config
        self.logger = logging.getLogger(__name__)

    def _ensure_even_dimensions(self, width: int, height: int) -> Tuple[int, int]:
        """Ensure dimensions are multiples of 2 (for video codec compatibility).

        Args:
            width: Image width in pixels
            height: Image height in pixels

        Returns:
            Tuple of (adjusted_width, adjusted_height) both divisible by 2
        """
        if not self.config.ensure_even:
            return width, height

        # Round down to nearest even number
        even_width = (width // 2) * 2
        even_height = (height // 2) * 2

        if even_width != width or even_height != height:
            self.logger.debug(
                "[ImagePreprocessor] Adjusted dimensions: %dx%d → %dx%d (multiples of 2)",
                width, height, even_width, even_height
            )

        return even_width, even_height

    def _detect_orientation(self, width: int, height: int) -> str:
        """Detect image orientation based on aspect ratio.

        Args:
            width: Image width
            height: Image height

        Returns:
            "portrait", "landscape", or "square"
        """
        aspect_ratio = width / height

        if abs(aspect_ratio - 1.0) < 0.1:  # Within 10% of square
            return "square"
        elif aspect_ratio < 1.0:
            return "portrait"
        else:
            return "landscape"

    def _calculate_target_dimensions(
        self,
        original_width: int,
        original_height: int
    ) -> Tuple[int, int]:
        """Calculate target dimensions respecting constraints.

        Priority order:
        1. Finik preset (if specified)
        2. target_resolution (if specified)
        3. max_dimension constraint
        4. min_dimension constraint
        5. ensure_even alignment

        Maintains aspect ratio unless target_resolution or preset is specified.

        Args:
            original_width: Original image width
            original_height: Original image height

        Returns:
            Tuple of (target_width, target_height)
        """
        # Priority 1: Finik preset
        if self.config.preset:
            orientation = self._detect_orientation(original_width, original_height)
            target_w, target_h = self.PRESETS[self.config.preset][orientation]

            self.logger.info(
                "[ImagePreprocessor] Preset: %s %s → %dx%d",
                self.config.preset, orientation, target_w, target_h
            )

            # For preset, we may need to pad or crop (handled in resize logic)
            return self._ensure_even_dimensions(target_w, target_h)

        # Priority 2: Specific target resolution
        if self.config.target_resolution:
            target_w, target_h = self.config.target_resolution
            self.logger.info(
                "[ImagePreprocessor] Target resolution: %dx%d → %dx%d",
                original_width, original_height,
                target_w, target_h
            )
            return self._ensure_even_dimensions(target_w, target_h)

        # Priority 3: Max dimension constraint
        max_dim = max(original_width, original_height)
        if self.config.max_dimension and max_dim > self.config.max_dimension:
            scale = self.config.max_dimension / max_dim
            new_width = int(original_width * scale)
            new_height = int(original_height * scale)
            new_width, new_height = self._ensure_even_dimensions(new_width, new_height)

            self.logger.info(
                "[ImagePreprocessor] Resize (max): %dx%d → %dx%d (scale=%.3f, max=%d)",
                original_width, original_height,
                new_width, new_height,
                scale, self.config.max_dimension
            )
            return new_width, new_height

        # Priority 4: Min dimension constraint
        min_dim = min(original_width, original_height)
        if self.config.min_dimension and min_dim < self.config.min_dimension:
            scale = self.config.min_dimension / min_dim
            new_width = int(original_width * scale)
            new_height = int(original_height * scale)
            new_width, new_height = self._ensure_even_dimensions(new_width, new_height)

            self.logger.info(
                "[ImagePreprocessor] Resize (min): %dx%d → %dx%d (scale=%.3f, min=%d)",
                original_width, original_height,
                new_width, new_height,
                scale, self.config.min_dimension
            )
            return new_width, new_height

        # Priority 5: Just ensure even dimensions (no resize)
        return self._ensure_even_dimensions(original_width, original_height)

    def preprocess(self, image_path: Path) -> Tuple[bytes, int, int]:
        """Preprocess image with configured constraints.

        Processing pipeline:
        1. Load image with PIL
        2. Convert to RGB if needed
        3. Calculate target dimensions (respects constraints)
        4. Resize if needed (fit/pad/crop based on resize_mode)
        5. Encode to JPG or preserve format
        6. Validate file size if max_file_size_mb set
        7. Return encoded bytes + final dimensions

        Args:
            image_path: Path to input image file

        Returns:
            Tuple of (encoded_bytes, width, height)

        Raises:
            FileNotFoundError: If image file doesn't exist
            ValueError: If image violates constraints and auto_resize is disabled
            RuntimeError: If encoded image exceeds max_file_size_mb
            OSError: If PIL cannot open/decode the image

        Example:
            >>> # Adapter defines its own config
            >>> config = ImagePreprocessorConfig(
            ...     max_dimension=1920,
            ...     convert_to_jpg=True,
            ...     ensure_even=True,
            ... )
            >>> preprocessor = ImagePreprocessor(config)
            >>> img_bytes, w, h = preprocessor.preprocess(Path("photo.png"))
            >>> print(f"Preprocessed: {w}x{h}, size={len(img_bytes)/1024:.1f}KB")
            Preprocessed: 1280x720, size=156.3KB
        """
        if not image_path.exists():
            raise FileNotFoundError(f"Image file not found: {image_path}")

        # Load image
        try:
            with Image.open(image_path) as img:
                # Convert RGBA/P to RGB if needed (for JPG compatibility)
                if img.mode in ('RGBA', 'P', 'LA'):
                    self.logger.debug(
                        "[ImagePreprocessor] Converting %s mode to RGB",
                        img.mode
                    )
                    # Create white background
                    rgb_img = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    rgb_img.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = rgb_img
                elif img.mode != 'RGB':
                    img = img.convert('RGB')

                original_width, original_height = img.size

                # Calculate target dimensions
                target_width, target_height = self._calculate_target_dimensions(
                    original_width, original_height
                )

                # Check if resize needed
                if (target_width, target_height) != (original_width, original_height):
                    if not self.config.auto_resize:
                        raise ValueError(
                            f"Image dimensions {original_width}x{original_height} "
                            f"violate constraints (target: {target_width}x{target_height}), "
                            f"and auto_resize is disabled"
                        )

                    # Resize using high-quality Lanczos resampling
                    # TODO: Implement pad/crop modes for Finik (currently just fit)
                    img = img.resize((target_width, target_height), Image.LANCZOS)
                    self.logger.info(
                        "[ImagePreprocessor] Resized: %dx%d → %dx%d",
                        original_width, original_height,
                        target_width, target_height
                    )

                # Encode to bytes
                buffer = io.BytesIO()

                if self.config.convert_to_jpg:
                    img.save(buffer, format='JPEG', quality=self.config.jpg_quality, optimize=True)
                    format_name = f"JPG (quality={self.config.jpg_quality})"
                else:
                    # Keep original format
                    img_format = img.format or 'PNG'
                    img.save(buffer, format=img_format)
                    format_name = img_format

                encoded_bytes = buffer.getvalue()
                size_mb = len(encoded_bytes) / (1024 * 1024)

                # Validate file size if constraint set
                if self.config.max_file_size_mb and size_mb > self.config.max_file_size_mb:
                    raise RuntimeError(
                        f"Encoded image size {size_mb:.2f}MB exceeds "
                        f"max_file_size_mb {self.config.max_file_size_mb}MB. "
                        f"Try reducing jpg_quality or dimensions."
                    )

                self.logger.info(
                    "[ImagePreprocessor] Preprocessed: %s → %dx%d, "
                    "format=%s, size=%.2fMB (%.1fKB)",
                    image_path.name,
                    target_width, target_height,
                    format_name,
                    size_mb, len(encoded_bytes) / 1024
                )

                return encoded_bytes, target_width, target_height

        except Exception as e:
            self.logger.error(
                "[ImagePreprocessor] Failed to preprocess %s: %s",
                image_path, e
            )
            raise
