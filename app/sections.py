"""Encoding of a chunk's section path for hierarchy-aware lookup.

``section_path`` is stored on every chunk as a JSON array of ancestor headings
(``["PERATURAN MENTERI KEUANGAN", "Pasal 16"]``). Answering "give me the content
of that heading" means finding the chunks that belong to it, so this module owns
the one encoding that makes those lookups indexable.

Why JSON text and not a joined string
-------------------------------------
The obvious encoding is ``"/".join(path)``. It breaks. A document with a heading
``"Pasal 16/17"`` produces the key ``"Pasal 16/17"``, which is byte-identical to
the key for the two-heading path ``["Pasal", "16/17"]``. A subtree query for
either would then silently return the other's chunks.

``json.dumps`` escapes the separators out of heading text, so distinct paths
always produce distinct keys. It also makes descendant matching exact rather
than heuristic: a JSON array's closing bracket is followed by a comma in every
strict descendant and by nothing in the array itself, so dropping the final
``]`` and appending ``,%`` matches descendants and only descendants.

Why not slice the stored jsonb
------------------------------
It is not possible. PostgreSQL rejects ``jsonb[0:1]`` outright ("jsonb subscript
does not support slices"), and the jsonpath spelling ``$[0] == "..."`` looks like
it works but matches far more than the prefix it names. Measured on a five-row
fixture, ``$[0] == "PMK"`` returned all five rows, including rows whose path
never mentioned ``PMK``. Both spellings are wrong for prefix semantics, which is
what a subtree query needs.
"""

import json

SECTION_KEY_SEPARATOR = ","

# LIKE's own metacharacters. A heading such as "Ayat (1)_a" is ordinary text in
# a document, but to LIKE the underscore is a single-character wildcard, so an
# unescaped key would match "Ayat (1)Xa" too. Backslash is listed first because
# it is the escape character itself and doubling it has to happen before the
# other two are touched.
LIKE_ESCAPE = "\\"
_LIKE_METACHARACTERS = ("\\", "%", "_")


def encode_section_key(section_path: list[str]) -> str:
    """Render a section path as the canonical text key stored on the chunk."""
    return json.dumps(
        section_path, separators=(SECTION_KEY_SEPARATOR, ":"), ensure_ascii=False
    )


def descendant_prefix(ancestor_key: str) -> str:
    """Literal text every key beneath ``ancestor_key`` starts with.

    A JSON array's closing bracket is followed by a comma in every strict
    descendant and by nothing in the array itself, so removing it and appending a
    separator yields a prefix that separates children from cousins: the subtree
    of ``["Pasal 16"]`` starts ``["Pasal 16",`` while the unrelated sibling
    ``["Pasal 160"]`` starts ``["Pasal 160`` and cannot match.
    """
    return ancestor_key[:-1] + SECTION_KEY_SEPARATOR


def subtree_pattern(ancestor_key: str) -> str:
    """LIKE pattern matching every strict descendant of an already-encoded key.

    Takes the encoded key rather than a path because that is the form retrieval
    has in hand: the anchors come from chunks already loaded from the database.
    The literal prefix is escaped, so callers must pass ``escape=LIKE_ESCAPE`` to
    the LIKE they build from it.
    """
    literal = descendant_prefix(ancestor_key)
    escaped = "".join(
        f"{LIKE_ESCAPE}{character}"
        if character in _LIKE_METACHARACTERS
        else character
        for character in literal
    )
    return f"{escaped}%"


def section_descends_from(child_key: str, ancestor_key: str) -> bool:
    """Whether ``child_key`` is ``ancestor_key`` or a section beneath it.

    This is the same rule :func:`subtree_pattern` expresses in SQL, kept beside it
    so the two cannot drift. Retrieval uses it to attribute a completed row back to
    the anchor that asked for it, which SQL cannot do for us: the row carries the
    descendant's key, not the anchor's.
    """
    if not ancestor_key:
        return False
    return child_key == ancestor_key or child_key.startswith(
        descendant_prefix(ancestor_key)
    )