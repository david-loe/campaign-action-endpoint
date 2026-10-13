"""Self-contained SVG graphics for uploaded branding logos."""
import re
from xml.etree import ElementTree

import tinycss2
from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
SVG_ELEMENTS = frozenset((
    "svg", "g", "a", "defs", "title", "desc", "symbol", "use", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "textPath", "clipPath", "mask", "pattern", "marker", "linearGradient", "radialGradient", "stop", "style",
    "image", "filter", "feBlend", "feColorMatrix", "feComponentTransfer", "feComposite", "feConvolveMatrix", "feDiffuseLighting",
    "feDisplacementMap", "feDistantLight", "feDropShadow", "feFlood", "feFuncA", "feFuncB", "feFuncG", "feFuncR", "feGaussianBlur",
    "feImage", "feMerge", "feMergeNode", "feMorphology", "feOffset", "fePointLight", "feSpecularLighting", "feSpotLight",
    "feTile", "feTurbulence",
))
ElementTree.register_namespace("", SVG_NAMESPACE)
ElementTree.register_namespace("xlink", "http://www.w3.org/1999/xlink")


def safe_reference(value):
    value = value.strip()
    return bool(re.fullmatch(r"#[^\s]+", value) or re.fullmatch(r"data:image/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=\s]+", value, re.I))


def safe_css(value):
    pending = list(tinycss2.parse_component_value_list(value))
    while pending:
        token = pending.pop()
        if token.type == "error" or token.type == "at-keyword" and token.lower_value in ("import", "namespace"):
            return False
        if token.type == "url" and not safe_reference(token.value):
            return False
        if token.type == "function":
            if token.lower_name == "expression":
                return False
            if token.lower_name == "url":
                args = [arg for arg in token.arguments if arg.type not in ("whitespace", "comment")]
                if len(args) != 1 or args[0].type != "string" or not safe_reference(args[0].value):
                    return False
            pending.extend(token.arguments)
        pending.extend(getattr(token, "content", ()))
    return True


def sanitize_svg(data):
    try:
        root = SafeElementTree.fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except (ElementTree.ParseError, DefusedXmlException, ValueError) as exc:
        raise ValueError("Bitte laden Sie ein gültiges PNG-, JPEG-, WebP- oder SVG-Logo hoch.") from exc
    if root.tag not in ("svg", f"{{{SVG_NAMESPACE}}}svg"):
        raise ValueError("Bitte laden Sie ein gültiges PNG-, JPEG-, WebP- oder SVG-Logo hoch.")
    # Retain static drawing elements and local references, including class styles
    # and gradients; scripts, animation and foreign documents are discarded.
    pending = [root]
    while pending:
        element = pending.pop()
        if not element.tag.startswith("{"):
            element.tag = f"{{{SVG_NAMESPACE}}}{element.tag}"
        local_name = element.tag.rsplit("}", 1)[-1]
        for attribute, value in list(element.attrib.items()):
            name = attribute.rsplit("}", 1)[-1].lower()
            if (name.startswith("on") or name == "base" or name in ("href", "src") and not safe_reference(value)
                    or not safe_css(value)):
                del element.attrib[attribute]
        if local_name == "style" and not safe_css(element.text or ""):
            element.text = ""
        for child in list(element):
            tag = child.tag
            namespace, name = tag[1:].split("}", 1) if tag.startswith("{") else (SVG_NAMESPACE, tag)
            if namespace != SVG_NAMESPACE or name not in SVG_ELEMENTS:
                element.remove(child)
            else:
                pending.append(child)
    try:
        return ElementTree.tostring(root, encoding="utf-8")
    except RecursionError as exc:
        raise ValueError("Das SVG-Logo ist zu stark verschachtelt.") from exc
