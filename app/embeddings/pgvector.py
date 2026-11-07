"""pgvector's text form for one embedding.

asyncpg ships no native codec for the ``vector`` type, so every statement
that binds a vector sends it as a string and casts inside the SQL.
"""


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"
