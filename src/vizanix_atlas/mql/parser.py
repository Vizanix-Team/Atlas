"""MQL recursive-descent parser.

Implements the grammar documented in ``docs/MQL.md``::

    query        := SELECT select_list FROM ident where? order_by? limit? ';'?
    select_list  := '*' | select_item (',' select_item)*
    select_item  := ident ('AS' ident)?
    where        := 'WHERE' bool_expr
    order_by     := 'ORDER' 'BY' order_item (',' order_item)*
    order_item   := ident ('ASC' | 'DESC')?
    limit        := 'LIMIT' number

    bool_expr    := or_expr
    or_expr      := and_expr ('OR' and_expr)*
    and_expr     := not_expr ('AND' not_expr)*
    not_expr     := 'NOT' not_expr | predicate
    predicate    := '(' bool_expr ')'
                  | operand 'IS' 'NOT'? 'NULL'
                  | operand 'NOT'? 'IN' '(' literal (',' literal)* ')'
                  | operand 'NOT'? 'BETWEEN' operand 'AND' operand
                  | operand comparison_op operand
    operand      := ident | literal
    literal      := '-'? number | string | 'TRUE' | 'FALSE' | 'NULL'

A hand-written parser rather than a grammar-generator dependency, so the language
stays exactly this small on purpose: every production above is one function below,
and adding a new one is a deliberate, reviewable change rather than a side effect of a
grammar file growing.
"""

from __future__ import annotations

from vizanix_atlas.core.errors import MqlSyntaxError
from vizanix_atlas.mql.ast_nodes import (
    And,
    Between,
    BoolExpr,
    Comparison,
    ComparisonOperator,
    Identifier,
    InList,
    IsNull,
    Literal_,
    Not,
    Operand,
    Or,
    OrderItem,
    Query,
    SelectItem,
)
from vizanix_atlas.mql.lexer import Token, TokenType, tokenise

_COMPARISON_TOKENS: dict[TokenType, ComparisonOperator] = {
    TokenType.EQ: "=",
    TokenType.NEQ: "!=",
    TokenType.LT: "<",
    TokenType.LE: "<=",
    TokenType.GT: ">",
    TokenType.GE: ">=",
}


class Parser:
    """Consumes a token stream and produces a :class:`Query`.

    One parser instance per query; it is not reusable across calls, which keeps its
    position-tracking state simple.
    """

    def __init__(self, tokens: list[Token]) -> None:
        self._tokens = tokens
        self._index = 0

    @property
    def _current(self) -> Token:
        return self._tokens[self._index]

    def _advance(self) -> Token:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _expect(self, token_type: TokenType) -> Token:
        """Consume and return a token of ``token_type``.

        Raises:
            MqlSyntaxError: If the current token is not of the expected type.
        """
        if self._current.type is not token_type:
            found = repr(self._current.text) if self._current.text else "<end of query>"
            raise MqlSyntaxError(
                f"expected {token_type.value.upper()} but found {found}",
                position=self._current.position,
            )
        return self._advance()

    def parse(self) -> Query:
        """Parse a complete query.

        Raises:
            MqlSyntaxError: On any grammar violation, or on trailing input after a
                complete query (other than an optional final ``;``).
        """
        query = self._parse_query()
        if self._current.type is TokenType.SEMICOLON:
            self._advance()
        if self._current.type is not TokenType.EOF:
            raise MqlSyntaxError(
                f"unexpected input after the end of the query: {self._current.text!r}",
                position=self._current.position,
            )
        return query

    def _parse_query(self) -> Query:
        self._expect(TokenType.SELECT)
        select_star, select_items = self._parse_select_list()
        self._expect(TokenType.FROM)
        source = self._parse_identifier()

        where: BoolExpr | None = None
        if self._current.type is TokenType.WHERE:
            self._advance()
            where = self._parse_or_expr()

        order_by: tuple[OrderItem, ...] = ()
        if self._current.type is TokenType.ORDER:
            self._advance()
            self._expect(TokenType.BY)
            order_by = self._parse_order_list()

        limit: int | None = None
        if self._current.type is TokenType.LIMIT:
            self._advance()
            limit_position = self._current.position
            negated = False
            if self._current.type is TokenType.MINUS:
                self._advance()
                negated = True
            limit_token = self._expect(TokenType.NUMBER)
            limit = int(float(limit_token.text))
            if negated:
                limit = -limit
            if limit < 0:
                raise MqlSyntaxError("LIMIT must not be negative", position=limit_position)

        return Query(
            select_star=select_star,
            select_items=select_items,
            source=source,
            where=where,
            order_by=order_by,
            limit=limit,
        )

    def _parse_select_list(self) -> tuple[bool, tuple[SelectItem, ...]]:
        if self._current.type is TokenType.STAR:
            self._advance()
            return True, ()
        items = [self._parse_select_item()]
        while self._current.type is TokenType.COMMA:
            self._advance()
            items.append(self._parse_select_item())
        return False, tuple(items)

    def _parse_select_item(self) -> SelectItem:
        name = self._parse_identifier()
        alias: str | None = None
        if self._current.type is TokenType.AS:
            self._advance()
            alias = self._expect(TokenType.IDENT).text
        return SelectItem(name=name, alias=alias)

    def _parse_order_list(self) -> tuple[OrderItem, ...]:
        items = [self._parse_order_item()]
        while self._current.type is TokenType.COMMA:
            self._advance()
            items.append(self._parse_order_item())
        return tuple(items)

    def _parse_order_item(self) -> OrderItem:
        name = self._parse_identifier()
        descending = False
        if self._current.type is TokenType.ASC:
            self._advance()
        elif self._current.type is TokenType.DESC:
            self._advance()
            descending = True
        return OrderItem(name=name, descending=descending)

    def _parse_or_expr(self) -> BoolExpr:
        left = self._parse_and_expr()
        while self._current.type is TokenType.OR:
            self._advance()
            left = Or(left=left, right=self._parse_and_expr())
        return left

    def _parse_and_expr(self) -> BoolExpr:
        left = self._parse_not_expr()
        while self._current.type is TokenType.AND:
            self._advance()
            left = And(left=left, right=self._parse_not_expr())
        return left

    def _parse_not_expr(self) -> BoolExpr:
        if self._current.type is TokenType.NOT:
            self._advance()
            return Not(operand=self._parse_not_expr())
        return self._parse_predicate()

    def _parse_predicate(self) -> BoolExpr:
        if self._current.type is TokenType.LPAREN:
            self._advance()
            inner = self._parse_or_expr()
            self._expect(TokenType.RPAREN)
            return inner

        left = self._parse_operand()

        if self._current.type is TokenType.IS:
            self._advance()
            negated = False
            if self._current.type is TokenType.NOT:
                self._advance()
                negated = True
            self._expect(TokenType.NULL)
            return IsNull(operand=left, negated=negated)

        negated = False
        if self._current.type is TokenType.NOT:
            self._advance()
            negated = True

        if self._current.type is TokenType.IN:
            self._advance()
            self._expect(TokenType.LPAREN)
            values = [self._parse_literal()]
            while self._current.type is TokenType.COMMA:
                self._advance()
                values.append(self._parse_literal())
            self._expect(TokenType.RPAREN)
            return InList(operand=left, values=tuple(values), negated=negated)

        if self._current.type is TokenType.BETWEEN:
            self._advance()
            low = self._parse_operand()
            self._expect(TokenType.AND)
            high = self._parse_operand()
            return Between(operand=left, low=low, high=high, negated=negated)

        if negated:
            raise MqlSyntaxError(
                "NOT must be followed by IN, BETWEEN, or another condition",
                position=self._current.position,
            )

        operator_type = self._current.type
        operator = _COMPARISON_TOKENS.get(operator_type)
        if operator is None:
            raise MqlSyntaxError(
                "expected a comparison operator, IS, IN, or BETWEEN",
                position=self._current.position,
            )
        self._advance()
        right = self._parse_operand()
        return Comparison(left=left, operator=operator, right=right)

    def _parse_operand(self) -> Operand:
        if self._current.type is TokenType.IDENT:
            return self._parse_identifier()
        return self._parse_literal()

    def _parse_identifier(self) -> Identifier:
        token = self._expect(TokenType.IDENT)
        return Identifier(name=token.text, position=token.position)

    def _parse_literal(self) -> Literal_:
        token = self._current
        if token.type is TokenType.MINUS:
            self._advance()
            number = self._expect(TokenType.NUMBER)
            return Literal_(value=-float(number.text), position=token.position)
        if token.type is TokenType.NUMBER:
            self._advance()
            return Literal_(value=float(token.text), position=token.position)
        if token.type is TokenType.STRING:
            self._advance()
            return Literal_(value=token.text, position=token.position)
        if token.type is TokenType.TRUE:
            self._advance()
            return Literal_(value=True, position=token.position)
        if token.type is TokenType.FALSE:
            self._advance()
            return Literal_(value=False, position=token.position)
        if token.type is TokenType.NULL:
            self._advance()
            return Literal_(value=None, position=token.position)
        found = repr(token.text) if token.text else "<end of query>"
        raise MqlSyntaxError(
            f"expected a literal value but found {found}",
            position=token.position,
        )


def parse_query(text: str) -> Query:
    """Parse an MQL query string into a :class:`Query`.

    Raises:
        MqlSyntaxError: On any lexical or grammatical error.
    """
    return Parser(tokenise(text)).parse()
