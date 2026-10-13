"""Shared validation and resolution for public page branding."""
import io
import re

from PIL import Image, UnidentifiedImageError

from .models import BrandingConfig, db
from .svg_logo import sanitize_svg

DEFAULT_ACCENT = "#2359dc"
MAX_LOGO_BYTES = 2 * 1024 * 1024
MAX_LOGO_DIMENSION = 4096
LOGO_TYPES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def validate_color(value):
    value = value.strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError("Bitte geben Sie eine Akzentfarbe im Format #RRGGBB an.")
    return value.lower()


def read_logo(upload):
    if upload is None or not upload.filename:
        return None
    data = upload.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise ValueError("Das Logo darf höchstens 2 MiB groß sein.")
    try:
        with Image.open(io.BytesIO(data), formats=list(LOGO_TYPES)) as image:
            if max(image.size) > MAX_LOGO_DIMENSION:
                raise ValueError("Das Logo darf höchstens 4096 × 4096 Pixel groß sein.")
            mime = LOGO_TYPES[image.format]
            image.verify()
        with Image.open(io.BytesIO(data), formats=list(LOGO_TYPES)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError):
        return sanitize_svg(data), "image/svg+xml"
    return data, mime


def apply_logo(target, logo, remove=False):
    if logo and remove:
        raise ValueError("Bitte wählen Sie entweder ein neues Logo oder die Entfernung des Logos.")
    if logo:
        target.logo_data, target.logo_mime = logo
    elif remove:
        target.logo_data = target.logo_mime = None


def global_branding():
    return db.session.get(BrandingConfig, 1)


def effective_branding(campaign):
    return campaign if campaign.branding_mode == "custom" else global_branding()


def public_branding(campaign):
    branding = effective_branding(campaign)
    return {"accent_color": branding.accent_color if branding else DEFAULT_ACCENT,
            "has_logo": bool(branding and branding.logo_mime)}


def luminance(rgb):
    channels = [value / 255 for value in rgb]
    linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in channels]
    return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))


def branding_css(color):
    # All interpolated values are derived from a validated six-digit hex color.
    color = validate_color(color)
    rgb = tuple(int(color[index:index + 2], 16) for index in (1, 3, 5))
    lightness = luminance(rgb)
    white_text = 1.05 / (lightness + .05) >= (lightness + .05) / .05
    foreground = "#ffffff" if white_text else "#000000"
    hover = tuple(round(value * .9) if white_text else round(value + (255 - value) * .1) for value in rgb)
    readable = rgb
    while 1.05 / (luminance(readable) + .05) < 4.5:
        readable = tuple(int(value * .9) for value in readable)
    dark_readable = rgb
    # Use the lightest dark surface used by public links (the preview notice).
    dark_surface_luminance = luminance((25, 44, 71))
    while ((max(luminance(dark_readable), dark_surface_luminance) + .05)
           / (min(luminance(dark_readable), dark_surface_luminance) + .05)) < 4.5:
        dark_readable = tuple(min(255, value + max(1, round((255 - value) * .1))) for value in dark_readable)
    hex_color = lambda values: "#" + "".join(f"{value:02x}" for value in values)
    return (f".public-page{{--blue:{color};--blue-dark:{hex_color(hover)};"
            f"--accent-text:{foreground};--accent-readable:{hex_color(readable)};}}"
            f"@media(prefers-color-scheme:dark){{.public-page{{--accent-readable:{hex_color(dark_readable)};}}}}")
