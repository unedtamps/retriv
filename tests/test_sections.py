import pytest

from app.sections import (
    LIKE_ESCAPE,
    descendant_prefix,
    encode_section_key,
    section_descends_from,
    subtree_pattern,
)


def test_a_joined_string_encoding_would_collide_but_json_does_not() -> None:
    """The reason section keys are JSON text rather than ``"/".join(path)``.

    Both of these are ordinary headings in a regulation. Joined on ``/`` they produce
    the same string, so a subtree query for one would return the other's chunks.
    """
    single_heading = encode_section_key(["Pasal 16/17"])
    two_headings = encode_section_key(["Pasal", "16/17"])

    assert single_heading != two_headings
    assert not section_descends_from(two_headings, single_heading)
    assert not section_descends_from(single_heading, two_headings)


def test_a_descendant_is_recognised_but_a_cousin_is_not() -> None:
    article = encode_section_key(["Pasal 16"])
    cousin = encode_section_key(["Pasal 160"])

    assert section_descends_from(article, article), "a section descends from itself"
    assert section_descends_from(encode_section_key(["Pasal 16", "16.01"]), article)
    assert not section_descends_from(cousin, article)
    assert not section_descends_from(encode_section_key(["Pasal 16", "16.01"]), cousin)


def test_an_empty_anchor_matches_nothing() -> None:
    """Sources with no section at all, such as CSV rows, must anchor nothing."""
    assert not section_descends_from(encode_section_key(["Pasal 16"]), "")
    assert not section_descends_from("", "")


def test_the_prefix_separates_children_from_cousins() -> None:
    """The trailing comma is what makes a prefix match exact rather than textual."""
    assert descendant_prefix(encode_section_key(["Pasal 16"])) == '["Pasal 16",'
    assert descendant_prefix(encode_section_key(["Pasal 160"])) == '["Pasal 160",'
    # A chunk with no headings encodes as the empty array; nothing nests under it.
    assert descendant_prefix(encode_section_key([])) == "[,"


def escape_literal(value: str) -> str:
    """Independent restatement of the escaping, so the test does not trust the code."""
    return "".join(
        f"{LIKE_ESCAPE}{character}" if character in "\\%_" else character
        for character in value
    )


@pytest.mark.parametrize("heading", ["Ayat (1)_a", "100%", "back\\slash", "plain"])
def test_like_metacharacters_in_a_heading_are_escaped(heading: str) -> None:
    """LIKE would otherwise read these as wildcards and widen the subtree.

    ``_`` matches any single character and ``%`` matches any run, so an unescaped key
    for "Ayat (1)_a" also matches "Ayat (1)Xa" and everything beneath it.
    """
    key = encode_section_key([heading])
    pattern = subtree_pattern(key)

    assert pattern == f"{escape_literal(descendant_prefix(key))}%"


def test_escaping_leaves_ordinary_headings_untouched() -> None:
    assert subtree_pattern(encode_section_key(["Pasal 16"])) == '["Pasal 16",%'