import typing
from collections import OrderedDict
from sqlparse import tokens, parse as sqlparse
from sqlparse.sql import Identifier, IdentifierList, Parenthesis, Function, Comparison, Where

from . import query, SQLStatement
from .functions import SQLFunc

from .operators import WhereOp
from . import SQLDecodeError, SQLToken


class Converter:
    def __init__(
            self,
            query: typing.Union[
                'query.SelectQuery',
                'query.BaseQuery'
            ],
            statement: SQLStatement
    ):
        self.query = query
        self.statement = statement
        self.end_id = None
        self.parse()

    def parse(self):
        raise NotImplementedError

    def to_mongo(self):
        raise NotImplementedError


class ColumnSelectConverter(Converter):
    def __init__(self, query_ref, statement):
        self.select_all = False
        self.return_const = None
        self.has_func = False
        self.num_columns = 0

        self.sql_tokens: typing.List[
            typing.Union[SQLToken, SQLFunc]
        ]= []
        super().__init__(query_ref, statement)

    def parse(self):
        tok = self.statement.next()
        if tok.value == '*':
            self.select_all = True

        elif isinstance(tok, Identifier):
            self._identifier(tok)

        elif isinstance(tok, IdentifierList):
            for atok in tok.get_identifiers():
                self._identifier(atok)

        elif tok.match(tokens.Keyword, 'DISTINCT'):
            self.query.distinct = DistinctConverter(self.query, self.statement)

        else:
            raise SQLDecodeError

    def _identifier(self, tok):
        if isinstance(tok[0], Parenthesis):
            self.return_const = int(tok[0][1].value)
            return

        elif isinstance(tok[0], Function):
            self.has_func = True
            func = SQLFunc(tok, self.query, self.query.token_alias)
            self.sql_tokens.append(func)

        else:
            sql = SQLToken(tok, self.query.token_alias)
            self.sql_tokens.append(sql)
            if sql.alias:
                self.query.token_alias.alias2token[sql.alias] = sql
                self.query.token_alias.token2alias[sql] = sql.alias

    def to_mongo(self):
        doc = [selected.column for selected in self.sql_tokens]
        return {'projection': doc}


class AggColumnSelectConverter(ColumnSelectConverter):

    def to_mongo(self):
        project = {}
        if self.return_const is not None:
            project['_const'] = {'$literal': self.return_const}

        elif self.has_func:
            # A SELECT func without groupby clause still needs a groupby
            # in MongoDB
            return self._using_group_by()

        else:
            for selected in self.sql_tokens:
                if selected.table == self.query.left_table:
                    project[selected.column] = True
                else:
                    project[f'{selected.table}.{selected.column}'] = True

        return [{'$project': project}]

    def _using_group_by(self):
        group = {
            '_id': None
        }
        project = {
            '_id': False
        }
        for selected in self.sql_tokens:
            if isinstance(selected, SQLFunc):
                group[selected.alias] = selected.to_mongo()
                project[selected.alias] = True
            else:
                if selected.table == self.query.left_table:
                    project[selected.column] = True
                else:
                    project[f'{selected.table}.{selected.column}'] = True

        pipeline = [
            {
                '$group': group
            },
            {
                '$project': project
            }
        ]

        return pipeline


class FromConverter(Converter):

    def parse(self):
        tok = self.statement.next()
        sql = SQLToken(tok, self.query.token_alias)
        self.query.left_table = sql.table
        if sql.alias:
            self.query.token_alias.alias2token[sql.alias] = sql
            self.query.token_alias.token2alias[sql] = sql.alias


class WhereConverter(Converter):
    nested_op: 'WhereOp' = None
    op: 'WhereOp' = None

    def parse(self):
        tok = self.statement.current_token
        self.op = WhereOp(
            statement=SQLStatement(tok),
            query=self.query,
            params=self.query.params
        )

    def to_mongo(self):
        return {'filter': self.op.to_mongo()}


class AggWhereConverter(WhereConverter):

    def to_mongo(self):
        return {'$match': self.op.to_mongo()}


class JoinConverter(Converter):
    def __init__(self, *args):
        self.left_table: str = None
        self.right_table: str = None
        self.left_column: str = None
        self.right_column: str = None
        super().__init__(*args)

    def parse(self):
        tok = self.statement.next()
        sql = SQLToken(tok, self.query.token_alias)
        right_table = self.right_table = sql.table
        if sql.alias:
            self.query.token_alias.alias2token[sql.alias] = sql
            self.query.token_alias.token2alias[sql] = sql.alias

        tok = self.statement.next()
        if not tok.match(tokens.Keyword, 'ON'):
            raise SQLDecodeError

        tok = self.statement.next()
        if isinstance(tok, Parenthesis):
            tok = tok[1]

        sql = SQLToken(tok, self.query.token_alias)
        if right_table == sql.right_table:
            self.left_table = sql.left_table
            self.left_column = sql.left_column
            self.right_column = sql.right_column
        else:
            self.left_table = sql.right_table
            self.left_column = sql.right_column
            self.right_column = sql.left_column

    def _lookup(self):
        if self.left_table == self.query.left_table:
            local_field = self.left_column
        else:
            local_field = f'{self.left_table}.{self.left_column}'

        lookup = {
            '$lookup': {
                'from': self.right_table,
                'localField': local_field,
                'foreignField': self.right_column,
                'as': self.right_table
            }
        }

        return lookup


class InnerJoinConverter(JoinConverter):

    def to_mongo(self):
        if self.left_table == self.query.left_table:
            match_field = self.left_column
        else:
            match_field = f'{self.left_table}.{self.left_column}'

        lookup = self._lookup()
        pipeline = [
            {
                '$match': {
                    match_field: {
                        '$ne': None,
                        '$exists': True
                    }
                }
            },
            lookup,
            {
                '$unwind': '$' + self.right_table
            }
        ]

        return pipeline


class OuterJoinConverter(JoinConverter):

    def _null_fields(self, table):
        toks = self.query.selected_columns.sql_tokens
        fields = {}
        for tok in toks:
            if tok.table == table:
                fields[tok.column] = None

        return fields

    def to_mongo(self):
        lookup = self._lookup()
        null_fields = self._null_fields(self.right_table)

        pipeline = [
            lookup,
            {
                '$unwind': {
                    'path': '$' + self.right_table,
                    'preserveNullAndEmptyArrays': True
                }
            },
            {
                '$addFields': {
                    self.right_table: {
                        '$ifNull': ['$'+self.right_table, null_fields]
                    }
                }
            }
        ]

        return pipeline


class LimitConverter(Converter):
    def __init__(self, *args):
        self.limit: int = None
        super().__init__(*args)

    def parse(self):
        tok = self.statement.next()
        self.limit = int(tok.value)

    def to_mongo(self):
        return {'limit': self.limit}


class AggLimitConverter(LimitConverter):

    def to_mongo(self):
        return {'$limit': self.limit}


class OrderConverter(Converter):
    def __init__(self, *args):
        self.columns: typing.List[typing.Tuple[SQLToken, SQLToken]] = []
        super().__init__(*args)

    def parse(self):
        tok = self.statement.next()
        if not tok.match(tokens.Keyword, 'BY'):
            raise SQLDecodeError

        tok = self.statement.next()
        if isinstance(tok, Identifier):
            self.columns.append(
                (SQLToken(tok[0], self.query.token_alias),
                 SQLToken(tok, self.query.token_alias)))

        elif isinstance(tok, IdentifierList):
            for _id in tok.get_identifiers():
                self.columns.append(
                    (SQLToken(_id[0], self.query.token_alias),
                     SQLToken(_id, self.query.token_alias)))

    def to_mongo(self):
        sort = [(tok.column, tok_ord.order) for tok, tok_ord in self.columns]
        return {'sort': sort}


class SetConverter(Converter):

    def __init__(self, *args):
        self.sql_tokens: typing.List[SQLToken] = []
        super().__init__(*args)

    def parse(self):
        tok = self.statement.next()

        if isinstance(tok, Comparison):
            self.sql_tokens.append(SQLToken(tok, self.query.token_alias))

        elif isinstance(tok, IdentifierList):
            for atok in tok.get_identifiers():
                self.sql_tokens.append((SQLToken(atok, self.query.token_alias)))

        else:
            raise SQLDecodeError

    def to_mongo(self):
        return {
            'update': {
                '$set': {
                    sql.lhs_column: self.query.params[sql.rhs_indexes]
                    if sql.rhs_indexes is not None else None
                    for sql in self.sql_tokens}
            }
        }


class AggOrderConverter(OrderConverter):

    def to_mongo(self):
        sort = OrderedDict()
        for tok, tok_ord in self.columns:
            if tok.has_parent():
                if tok.table == self.query.left_table:
                    field = tok.column
                else:
                    field = tok.table + '.' + tok.column
            else:
                field = tok.table
            sort[field] = tok_ord.order

        return {'$sort': sort}


class _Tokens2Id:

    def to_id(self):
        _id = {}
        for iden in self.sql_tokens:
            if iden.table == self.query.left_table:
                _id[iden.column] = f'${iden.column}'
            else:
                mongo_field = f'${iden.table}.{iden.column}'
                try:
                    _id[iden.table][iden.column] = mongo_field
                except KeyError:
                    _id[iden.table] = {iden.column: mongo_field}

        return _id


class DistinctConverter(ColumnSelectConverter, _Tokens2Id):
    def __init__(self, *args):
        super().__init__(*args)

    def to_mongo(self):
        _id = self.to_id()

        return [
            {
                '$group': {
                    '_id': _id
                }
            },
            {
                '$replaceRoot': {
                    'newRoot': '$_id'
                }
            }
        ]


class NestedInQueryConverter(Converter):

    def __init__(self, token, *args):
        self._token = token
        self._in_query: 'query.SelectQuery' = None
        super().__init__(*args)

    def parse(self):
        from .query import SelectQuery

        self._in_query = SelectQuery(
            self.query.db,
            self.query.connection_properties,
            sqlparse(self._token.value[1:-1])[0],
            self.query.params
        )

    def to_mongo(self):
        pipeline = [
            {
                '$lookup': {
                    'from': self._in_query.left_table,
                    'pipeline': self._in_query._make_pipeline(),
                    'as': '_nested_in'
                }
            },
            {
                '$addFields': {
                    '_nested_in': {
                        '$map': {
                            'input': '$_nested_in',
                            'as': 'lookup_result',
                            'in': '$$lookup_result.' + self._in_query.selected_columns.sql_tokens[0].column
                        }
                    }
                }
            }
        ]
        return pipeline


class HavingConverter(Converter):
    nested_op: 'WhereOp' = None
    op: 'WhereOp' = None

    def parse(self):
        tok = self.statement[:3]
        self.op = WhereOp(
            statement=tok,
            query=self.query,
            params=self.query.params
        )
        self.statement.skip(2)

    def to_mongo(self):
        return {'filter': self.op.to_mongo()}

class HavingConverter2(Converter):

    def parse(self):
        i = self.query.statement.value.find('HAVING')
        if i == -1:
            raise SQLDecodeError
        having = self.query.statement.value[i:]
        having = having.replace('HAVING', 'WHERE')
        having = sqlparse(having)[0][0]
        if not isinstance(having, Where):
            raise SQLDecodeError
        self.statement.skip(len(having.tokens) - 1)
        self._sub(having)
        having.value = str(having)
        self.op = WhereOp(
            token=having,
            query=self.query,
            params=self.query.params
        )

    def to_mongo(self):
        return {'$match': self.op.to_mongo()}

    def _sub(self, token):
        for i, child_token in enumerate(token.tokens):
            if isinstance(child_token, Parenthesis):
                self._sub(child_token)

            elif isinstance(child_token, Function):
                for func in self.query.selected_columns.sql_tokens:
                    if (isinstance(func, SQLFunc)
                            and func._token[0].value == child_token.value
                    ):
                        token.tokens[i] = sqlparse(
                            f'"{self.query.left_table}"."{func.alias}"'
                        )[0][0]
                        break
                else:
                    raise SQLDecodeError

            elif isinstance(child_token, Comparison):
                if isinstance(child_token[0], Function):
                    for func in self.query.selected_columns.sql_tokens:
                        if (isinstance(func, SQLFunc)
                           and func._token[0].value == child_token[0].value
                        ):
                            child_token.tokens[0] = sqlparse(
                                f'"{self.query.left_table}"."{func.alias}"'
                            )[0][0]
                            break
                    else:
                        raise SQLDecodeError


class GroupbyConverter(Converter, _Tokens2Id):

    def __init__(self, *args):
        self.sql_tokens: typing.List[SQLToken] = []
        super().__init__(*args)

    def parse(self):
        tok = self.statement.next()
        if not tok.match(tokens.Keyword, 'BY'):
            raise SQLDecodeError
        tok = self.statement.next()

        if isinstance(tok, Identifier):
            self.sql_tokens.append(SQLToken(tok, self.query.token_alias))
        else:
            for atok in tok.get_identifiers():
                self.sql_tokens.append(SQLToken(atok, self.query.token_alias))

    def to_mongo(self):
        _id = self.to_id()

        group = {
            '_id': _id
        }
        project = {
            '_id': False
        }
        for selected in self.query.selected_columns.sql_tokens:
            if isinstance(selected, SQLToken):
                if selected.table == self.query.left_table:
                    project[selected.column] = '$_id.' + selected.column
                else:
                    project[selected.table + '.' + selected.column] \
                        = f'_id.{selected.table}.{selected.column}'
            else:
                project[selected.alias] = True
                group[selected.alias] = selected.to_mongo()

        pipeline = [
            {
                '$group': group
            },
            {
                '$project': project
            }
        ]

        return pipeline


class OffsetConverter(Converter):
    def __init__(self, *args):
        self.offset: int = None
        super().__init__(*args)

    def parse(self):
        tok = self.statement.next()
        self.offset = int(tok.value)

    def to_mongo(self):
        return {'skip': self.offset}


class AggOffsetConverter(OffsetConverter):

    def to_mongo(self):
        return {'$skip': self.offset}
