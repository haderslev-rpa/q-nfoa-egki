"""Datamodeller til q-nfoa-egki."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class NfoaCaseLink:
    """Link til en konkret NFOA-sag."""

    cid: str
    url: str
    row_index: int
    row_text: str

    def to_dict(self) -> dict[str, Any]:
        """
        Konverterer resultatet til en dictionary.

        Returnerer:
            Dictionary med cid, URL, rækkeindeks og rækketekst.
        """
        return {
            "cid": self.cid,
            "url": self.url,
            "row_index": self.row_index,
            "row_text": self.row_text,
        }