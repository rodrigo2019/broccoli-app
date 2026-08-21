from __future__ import annotations

from random import Random

from broccoli_desktop.models import MAX_TITLE_LENGTH, validate_title
from broccoli_desktop.naming import (
    ADJECTIVES,
    FALLBACK_TITLE,
    PHRASES,
    SUBJECTS,
    SessionTitleGenerator,
    every_possible_title,
)


def test_every_title_the_lists_can_produce_is_a_valid_session_title() -> None:
    """A word list that grew too long would only fail once a user drew it."""
    titles = every_possible_title()

    assert titles
    for title in titles:
        assert validate_title(title) == title
        assert len(title) <= MAX_TITLE_LENGTH


def test_adjectives_agree_with_the_gender_of_their_subject() -> None:
    """ "Capivara Produtivo" is the failure this pairing exists to prevent."""
    generator = SessionTitleGenerator(random=Random(0))
    titles = {generator() for _ in range(200)}

    feminine = {subject for subject, is_feminine in SUBJECTS if is_feminine}
    masculine_forms = {masculine for masculine, _ in ADJECTIVES}
    for title in titles:
        subject = next((word for word in feminine if title.startswith(f"{word} ")), None)
        if subject is None:
            continue
        remainder = title[len(subject) + 1 :]
        assert remainder not in masculine_forms, title


def test_a_title_is_drawn_from_the_configured_words() -> None:
    generator = SessionTitleGenerator(random=Random(7))

    title = generator()

    subject = next(word for word, _ in SUBJECTS if title.startswith(f"{word} "))
    remainder = title[len(subject) + 1 :]
    forms = {form for pair in ADJECTIVES for form in pair} | set(PHRASES)
    assert remainder in forms


def test_recent_titles_are_not_handed_out_again() -> None:
    """Two identical names in a row are the repetition a user actually notices."""
    generator = SessionTitleGenerator(random=Random(3), memory=8)

    titles = [generator() for _ in range(8)]

    assert len(set(titles)) == len(titles)


def test_a_generator_with_nothing_left_to_draw_still_answers() -> None:
    """The caller needs a title, not an exception, when every retry collides."""

    class OneChoiceRandom(Random):
        def choice(self, sequence):  # type: ignore[override]
            return sequence[0]

        def random(self) -> float:
            return 0.0

    generator = SessionTitleGenerator(random=OneChoiceRandom(), memory=4, attempts=3)

    first = generator()
    second = generator()

    assert first not in {"", FALLBACK_TITLE}
    assert second == FALLBACK_TITLE


def test_two_generators_do_not_share_their_memory() -> None:
    """Module state here would make one test's draws leak into the next one."""
    seed = 11

    first = [SessionTitleGenerator(random=Random(seed))() for _ in range(3)]
    second = [SessionTitleGenerator(random=Random(seed))() for _ in range(3)]

    assert first == second
