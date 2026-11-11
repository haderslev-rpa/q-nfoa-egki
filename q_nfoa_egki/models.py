"""Datamodeller til NetForvaltning Offentligt Arrangement."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class NfoaCaseLink:
    """Link og identitet til en konkret NFOA-sag."""

    cid: str
    url: str
    row_index: int | None = None
    row_text: str = ""


@dataclass(frozen=True, slots=True)
class NfoaArrangementCase:
    """
    Normaliserede data fra en konkret NFOA-sag.

    fields indeholder de udlæste blanketfelter.

    Eksempel:
        {
            "kle": "...",
            "arrangement_navn": "...",
            "ansoegningsdato": "...",
        }
    """

    cid: str
    url: str
    blanket_type: str
    fields: dict[str, str] = field(
        default_factory=dict
    )

    def get(
        self,
        field_name: str,
        default: str = "",
    ) -> str:
        """
        Returnerer et blanketfelt som tekst.

        Hvis feltet ikke findes, returneres default.
        """

        value = self.fields.get(
            field_name,
            default,
        )

        if value is None:
            return default

        return str(
            value
        ).strip()