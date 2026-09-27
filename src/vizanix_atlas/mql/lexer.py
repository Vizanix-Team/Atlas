"""MQL tokeniser.

A small, fixed token set. There is no string concatenation, no comments, no
multi-statement input, and no way to spell an identifier that is not a plain word:
the lexer's job is partly to make the later stages' job easy, and partly to keep the
surface area small enough that "arbitrary SQL through the back door" has nowhere to
enter (see ``docs/MQL.md`` on why MQL is a controlled language, not a SQL passthrough).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Final

from vizanix_atlas.core.errors import MqlSyntaxError


class TokenType(StrEnum):
    """Every token MQL recognises."""

    SELECT = auto()
    FROM = auto()
    WHERE = auto()
    ORDER = auto()
    BY = auto()
    LIMIT = auto()
    AS = auto()
    AND = auto()
    OR = auto()
    NOT = auto()
    IN = auto()
    IS = auto()
    NULL = auto()
    BETWEEN = auto()
    ASC = auto()
    DESC = auto()
    TRUE = auto()
    FALSE = auto()

    IDENT = auto()
    NUMBER = auto()
    STRING = auto()
    STAR = auto()

    EQ = auto()
    NEQ = auto()
    LT = auto()
    LE = auto()
    GT = auto()
    GE = auto()

    COMMA = auto()
    LPAREN = auto()
    RPAREN = auto()
    SEMICOLON = auto()
    MINUS = auto()
    EOF = auto()


_KEYWORDS: Final = {
    "select": TokenType.SELECT,
    "from": TokenType.FROM,
    "where": TokenType.WHERE,
    "order": TokenType.ORDER,
    "by": TokenType.BY,
    "limit": TokenType.LIMIT,
    "as": TokenType.AS,
    "and": TokenType.AND,
    "or": TokenType.OR,
    "not": TokenType.NOT,
    "in": TokenType.IN,
    "is": TokenType.IS,
    "null": TokenType.NULL,
    "between": TokenType.BETWEEN,
    "asc": TokenType.ASC,
    "desc": TokenType.DESC,
    "true": TokenType.TRUE,
    "false": TokenType.FALSE,
}

#: Matched in order; the first alternative to match at the current position wins.
_TOKEN_PATTERN: Final = re.compile(
    r"""
    (?P<WS>\s+)
    |(?P<NUMBER>\d+\.\d+|\d+)
    |(?P<STRING>'(?:[^'\\]|\\.)*')
    |(?P<IDENT>[A-Za-z_][A-Za-z0-9_]*)
    |(?P<NEQ><>|!=)
    |(?P<LE><=)
    |(?P<GE>>=)
    |(?P<LT><)
    |(?P<GT>>)
    |(?P<EQ>=)
    |(?P<STAR>\*)
    |(?P<COMMA>,)
    |(?P<LPAREN>\()
    |(?P<RPAREN>\))
    |(?P<SEMICOLON>;)
    |(?P<MINUS>-)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class Token:
    """One lexical token."""

    type: TokenType
    text: str
    position: int


def tokenise(text: str) -> list[Token]:
    """Tokenise a query string.

    Raises:
        MqlSyntaxError: On any character the grammar does not recognise, with the
            character's position for caret rendering.
    """
    tokens: list[Token] = []
    position = 0
    length = len(text)
    while position < length:
        match = _TOKEN_PATTERN.match(text, position)
        if match is None:
            raise MqlSyntaxError(
                f"unrecognised character {text[position]!r}", position=position
            )
        position = match.end()
        kind = match.lastgroup
        value = match.group()
        if kind == "WS":
            continue
        if kind == "IDENT":
            keyword = _KEYWORDS.get(value.lower())
            token_type = keyword if keyword is not None else TokenType.IDENT
            tokens.append(Token(token_type, value, match.start()))
        elif kind == "STRING":
            # Strip the quotes and unescape \' and \\, the only two escapes a single
            # -quoted MQL string literal supports.
            inner = value[1:-1].replace("\\'", "'").replace("\\\\", "\\")
            tokens.append(Token(TokenType.STRING, inner, match.start()))
        else:
            tokens.append(Token(TokenType(kind.lower()), value, match.start()))
    tokens.append(Token(TokenType.EOF, "", length))
    return tokens
