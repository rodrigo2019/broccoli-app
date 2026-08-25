"""Friendly default titles for a session the user has not named yet.

A meeting the user never renames still has to be findable in the history, and a
UUID is not findable. The names are built from word lists rather than pulled
from a package: nothing compatible is vendored here, the desktop must work
offline, and a handful of literals is easier to keep grammatical in pt-BR than a
generic library would be.

Deliberately not translated with the rest of the interface. A title is written
into the session the moment it is created and then travels with it -- it is the
meeting's data, not a label on a control -- so translating it would only change
what the next session is called, while leaving every existing one as it was and
making a history that reads in two languages at once.

Grammar is the reason subjects carry a gender: "Pinguim Produtivo" and "Capivara
Produtiva" are both correct, "Capivara Produtivo" is not. Prepositional phrases
agree with anything, so they are listed once.
"""

from __future__ import annotations

from collections import deque
from random import Random

#: Subject and whether it takes the feminine form of an adjective.
SUBJECTS: tuple[tuple[str, bool], ...] = (
    ("Capivara", True),
    ("Pinguim", False),
    ("Jacaré", False),
    ("Lontra", True),
    ("Tucano", False),
    ("Coruja", True),
    ("Preguiça", True),
    ("Tatu", False),
    ("Golfinho", False),
    ("Arara", True),
    ("Quati", False),
    ("Onça", True),
    ("Café", False),
    ("Pão de Queijo", False),
    ("Brigadeiro", False),
    ("Chimarrão", False),
    ("Farofa", True),
    ("Goiabada", True),
    ("Cafezinho", False),
    ("Pastel de Feira", False),
)

#: Masculine and feminine forms of each adjective, in that order.
ADJECTIVES: tuple[tuple[str, str], ...] = (
    ("Produtivo", "Produtiva"),
    ("Turbinado", "Turbinada"),
    ("Distraído", "Distraída"),
    ("Filosófico", "Filosófica"),
    ("Apressado", "Apressada"),
    ("Sonolento", "Sonolenta"),
    ("Caprichoso", "Caprichosa"),
    ("Elétrico", "Elétrica"),
    ("Metódico", "Metódica"),
    ("Destemido", "Destemida"),
    ("Estratégico", "Estratégica"),
    ("Inspirado", "Inspirada"),
)

#: Prepositional phrases, which agree with every subject as written.
PHRASES: tuple[str, ...] = (
    "em Modo Turbo",
    "com Bugs",
    "na Segunda-feira",
    "sem Café",
    "em Reunião",
    "no Deploy",
    "de Plantão",
    "em Pauta",
    "com Pressa",
    "no Modo Foco",
    "fora do Ar",
    "em Retrospectiva",
)

#: Used when every retry collided with a recent title. Any name beats none.
FALLBACK_TITLE = "Nova sessão"


class SessionTitleGenerator:
    """Draw readable session titles, avoiding the ones just handed out.

    The memory is per instance rather than module state so a test can exercise
    repetition without leaking into the next one.
    """

    def __init__(
        self, *, random: Random | None = None, memory: int = 12, attempts: int = 8
    ) -> None:
        self._random = random or Random()
        self._recent: deque[str] = deque(maxlen=memory)
        self._attempts = attempts

    def __call__(self) -> str:
        """Return a title that is not among the most recent ones, if possible."""
        for _ in range(self._attempts):
            title = self._draw()
            if title not in self._recent:
                self._recent.append(title)
                return title
        return FALLBACK_TITLE

    def _draw(self) -> str:
        subject, feminine = self._random.choice(SUBJECTS)
        if self._random.random() < 0.5:
            masculine_form, feminine_form = self._random.choice(ADJECTIVES)
            return f"{subject} {feminine_form if feminine else masculine_form}"
        return f"{subject} {self._random.choice(PHRASES)}"


def every_possible_title() -> list[str]:
    """Every title the lists can produce, for the length check in the tests."""
    titles = []
    for subject, feminine in SUBJECTS:
        for masculine_form, feminine_form in ADJECTIVES:
            titles.append(f"{subject} {feminine_form if feminine else masculine_form}")
        titles.extend(f"{subject} {phrase}" for phrase in PHRASES)
    return titles
