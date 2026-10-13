"""Personalized plain text: parse a small Jinja subset without executing templates."""
import json
from dataclasses import dataclass
from functools import lru_cache

from jinja2 import Environment, TemplateSyntaxError, nodes

ROOTS = frozenset(("name", "email", "identity", "attributes", "source"))
parser = Environment()


@dataclass(frozen=True)
class Placeholder:
    path: tuple
    fallback: str | None = None


def data_path(expression):
    segments = []
    while isinstance(expression, (nodes.Getattr, nodes.Getitem)):
        if isinstance(expression, nodes.Getattr):
            segments.append(expression.attr)
        else:
            key = expression.arg
            if not isinstance(key, nodes.Const) or not (isinstance(key.value, str) or type(key.value) is int and key.value >= 0):
                raise ValueError("Platzhalter erlauben nur feste Schlüsselnamen und nicht negative Listenindizes.")
            segments.append(key.value)
        expression = expression.node
    if not isinstance(expression, nodes.Name) or expression.name not in ROOTS:
        raise ValueError("Platzhalter erlauben nur name, email, identity, attributes und source.")
    return (expression.name, *reversed(segments))


@lru_cache(maxsize=256)
def parse_confirmation_text(source):
    if "{%" in source or "{#" in source:
        raise ValueError("Der Bestätigungstext erlaubt nur Platzhalter und default-Ersatzwerte; keine Anweisungen oder Kommentare.")
    try:
        template = parser.parse(source)
    except TemplateSyntaxError as exc:
        raise ValueError(f"Ungültiger Platzhalter im Bestätigungstext (Zeile {exc.lineno}). Bitte prüfen Sie Klammern und Anführungszeichen.") from exc
    except RecursionError as exc:
        raise ValueError("Der Platzhalter im Bestätigungstext ist zu stark verschachtelt.") from exc
    parts = []
    for output in template.body:
        if not isinstance(output, nodes.Output):
            raise ValueError("Der Bestätigungstext erlaubt nur Text und Platzhalter.")
        for expression in output.nodes:
            if isinstance(expression, nodes.TemplateData):
                parts.append(expression.data)
                continue
            fallback = None
            if isinstance(expression, nodes.Filter):
                if (expression.name != "default" or len(expression.args) != 1 or expression.kwargs
                        or expression.dyn_args is not None or expression.dyn_kwargs is not None
                        or not isinstance(expression.args[0], nodes.Const) or not isinstance(expression.args[0].value, str)):
                    raise ValueError('Nur default("Ersatztext") ist als Filter erlaubt.')
                fallback = expression.args[0].value
                expression = expression.node
            parts.append(Placeholder(data_path(expression), fallback))
    return tuple(parts)


def resolve_path(context, path):
    value = context
    for key in path:
        if isinstance(value, dict) and isinstance(key, str):
            value = value.get(key)
        elif isinstance(value, list) and type(key) is int and 0 <= key < len(value):
            value = value[key]
        else:
            return None
    return value


def render_confirmation_text(source, recipient=None):
    context = {"name": recipient.name if recipient else "", "email": recipient.email if recipient else "",
               "identity": recipient.identity if recipient else "", "attributes": recipient.attributes if recipient else {},
               "source": recipient.source if recipient else {}}
    result = []
    for part in parse_confirmation_text(source):
        if isinstance(part, str):
            result.append(part)
            continue
        value = resolve_path(context, part.path)
        if value is None or value == "":
            result.append(part.fallback or "")
        elif isinstance(value, str):
            result.append(value)
        else:
            result.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return "".join(result)
