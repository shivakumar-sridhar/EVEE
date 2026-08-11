"""Vetted parametric templates.

Every buildable shape lives here behind a Pydantic params model. Nothing in this
pipeline generates freeform geometry: the extraction model (Phase 4) picks a
template name and fills its params, and that is the whole of its authority.

``TemplateSpec.description`` is written to be read by a model — Phase 4 renders
the registry straight into the extraction prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from build123d import Part
from pydantic import BaseModel

from vtp.templates import box

__all__ = ["TEMPLATE_REGISTRY", "TemplateSpec", "UnknownTemplateError", "get_template"]


class UnknownTemplateError(KeyError):
    """No template registered under that name."""


@dataclass(frozen=True)
class TemplateSpec:
    """Everything the pipeline needs to know about one template."""

    name: str
    description: str
    params_model: type[BaseModel]
    build: Callable[[BaseModel], tuple[Part, ...]]
    part_names: tuple[str, ...]
    #: Gate 1 read-back, templated in Python from validated params.
    spec_sentence: Callable[[BaseModel], str]
    #: Usable interior (l, w, h) in mm, or None for templates without an interior.
    inner_dims: Callable[[BaseModel], tuple[float, float, float] | None]

    def validate_params(self, params: dict) -> BaseModel:
        """Coerce a raw dict into this template's params model."""
        return self.params_model.model_validate(params)


TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {
    "box_with_lid": TemplateSpec(
        name="box_with_lid",
        description=(
            "A rectangular box with a separate press-fit lid. Use for storage "
            "boxes, enclosures, trays, parts bins, and containers. Dimensions "
            "given are OUTER unless the request says otherwise; the usable "
            "interior is smaller by the wall thickness. Produces two printable "
            "solids: the body and the lid."
        ),
        params_model=box.BoxWithLidParams,
        build=box.build,
        part_names=box.PART_NAMES,
        spec_sentence=box.resolved_spec_sentence,
        inner_dims=box.inner_dims,
    ),
}


def get_template(name: str) -> TemplateSpec:
    """Look up a template, with a message listing what does exist."""
    try:
        return TEMPLATE_REGISTRY[name]
    except KeyError:
        raise UnknownTemplateError(
            f"no template named {name!r}; registered templates are "
            f"{sorted(TEMPLATE_REGISTRY)}"
        ) from None
