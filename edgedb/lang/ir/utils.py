##
# Copyright (c) 2015-present MagicStack Inc.
# All rights reserved.
#
# See LICENSE for details.
##


import collections

from edgedb.lang.common import ast

from edgedb.lang.schema import lproperties as s_lprops
from edgedb.lang.schema import objects as s_obj
from edgedb.lang.schema import pointers as s_pointers
from edgedb.lang.schema import sources as s_src
from edgedb.lang.schema import types as s_types

from . import ast as irast


class PathIndex(collections.OrderedDict):
    """Graph path mapping path identifiers to AST nodes."""

    def update(self, other):
        for k, v in other.items():
            if k in self:
                super().__getitem__(k).update(v)
            else:
                self[k] = v

    def __setitem__(self, key, value):
        if not isinstance(key, (LinearPath, str)):
            raise TypeError('Invalid key type for PathIndex: %s' % key)

        if not isinstance(value, set):
            value = {value}

        super().__setitem__(key, value)


def infer_arg_types(ir, schema):
    def flt(n):
        if isinstance(n, irast.BinOp):
            return (isinstance(n.left, irast.Parameter) or
                    isinstance(n.right, irast.Parameter))

    ops = ast.find_children(ir, flt)

    arg_types = {}

    for binop in ops:
        typ = None

        if isinstance(binop.right, irast.Parameter):
            expr = binop.left
            arg = binop.right
            reversed = False
        else:
            expr = binop.right
            arg = binop.left
            reversed = True

        if isinstance(binop.op, irast.EdgeDBMatchOperator):
            typ = schema.get('std::str')

        elif isinstance(binop.op, (ast.ops.ComparisonOperator,
                                   ast.ops.ArithmeticOperator)):
            typ = infer_type(expr, schema)

        elif isinstance(binop.op, ast.ops.MembershipOperator) and not reversed:
            from edgedb.lang.schema import objects as s_obj

            elem_type = infer_type(expr, schema)
            typ = s_obj.Set(element_type=elem_type)

        elif isinstance(binop.op, ast.ops.BooleanOperator):
            typ = schema.get('std::bool')

        else:
            msg = 'cannot infer expr type: unsupported ' \
                  'operator: {!r}'.format(binop.op)
            raise ValueError(msg)

        if typ is None:
            msg = 'cannot infer expr type'
            raise ValueError(msg)

        try:
            existing = arg_types[arg.name]
        except KeyError:
            arg_types[arg.name] = typ
        else:
            if existing != typ:
                msg = 'cannot infer expr type: ambiguous resolution: ' + \
                      '{!r} and {!r}'
                raise ValueError(msg.format(existing, typ))

    return arg_types


def infer_type(ir, schema):
    if isinstance(ir, (irast.Set, irast.Shape)):
        result = ir.scls

    elif isinstance(ir, irast.FunctionCall):
        result = ir.func.returntype

        def is_polymorphic(t):
            if isinstance(t, s_obj.Collection):
                t = t.get_element_type()

            return t.name == 'std::any'

        if is_polymorphic(result):
            # Polymorhic function, determine the result type from
            # the argument type.
            for i, arg in enumerate(ir.args):
                if is_polymorphic(ir.func.paramtypes[i]):
                    result = infer_type(arg, schema)
                    break

    elif isinstance(ir, (irast.Constant, irast.Parameter)):
        result = ir.type

    elif isinstance(ir, irast.BinOp):
        if isinstance(ir.op, (ast.ops.ComparisonOperator,
                              ast.ops.TypeCheckOperator,
                              ast.ops.MembershipOperator,
                              irast.TextSearchOperator)):
            result = schema.get('std::bool')
        else:
            left_type = infer_type(ir.left, schema)
            right_type = infer_type(ir.right, schema)

            result = s_types.TypeRules.get_result(
                ir.op, (left_type, right_type), schema)
            if result is None:
                result = s_types.TypeRules.get_result(
                    (ir.op, 'reversed'), (right_type, left_type), schema)

    elif isinstance(ir, irast.UnaryOp):
        if ir.op == ast.ops.NOT:
            result = schema.get('std::bool')
        else:
            operand_type = infer_type(ir.expr, schema)
            result = s_types.TypeRules.get_result(
                ir.op, (operand_type,), schema)

    elif isinstance(ir, irast.IfElseExpr):
        if_expr = infer_type(ir.if_expr, schema)
        else_expr = infer_type(ir.else_expr, schema)

        if if_expr == else_expr:
            result = if_expr
        else:
            result = None

    elif isinstance(ir, (irast.TypeCast, irast.TypeFilter)):
        if ir.type.subtypes:
            coll = s_obj.Collection.get_class(ir.type.maintype)
            result = coll.from_subtypes(
                [schema.get(t) for t in ir.type.subtypes])
        else:
            result = schema.get(ir.type.maintype)

    elif isinstance(ir, irast.Stmt):
        result = infer_type(ir.result, schema)

    elif isinstance(ir, irast.ExistPred):
        result = schema.get('std::bool')

    elif isinstance(ir, irast.SliceIndirection):
        result = infer_type(ir.expr, schema)

    elif isinstance(ir, irast.IndexIndirection):
        arg = infer_type(ir.expr, schema)

        if arg is None:
            result = None
        else:
            str_t = schema.get('std::str')
            if arg.issubclass(str_t):
                result = arg
            else:
                result = None

    elif isinstance(ir, irast.Sequence):
        if ir.is_array:
            result = s_obj.Array(element_type=schema.get('std::any'))
        else:
            result = s_obj.Tuple(element_type=schema.get('std::any'))

    else:
        result = None

    if result is not None:
        allowed = (s_obj.Class, s_obj.MetaClass)
        if not (isinstance(result, allowed) or
                (isinstance(result, (tuple, list)) and
                 isinstance(result[1], allowed))):
            raise RuntimeError(
                f'infer_type({ir!r}) retured {result!r} instead of a Class')

    return result


def get_source_references(ir):
    result = set()

    flt = lambda n: isinstance(n, irast.Set) and n.expr is None
    ir_sets = ast.find_children(ir, flt)
    for ir_set in ir_sets:
        result.add(ir_set.scls)

    return result


def get_terminal_references(ir):
    result = set()
    parents = set()

    flt = lambda n: isinstance(n, irast.Set) and n.expr is None
    ir_sets = ast.find_children(ir, flt)
    for ir_set in ir_sets:
        result.add(ir_set)
        if ir_set.rptr:
            parents.add(ir_set.rptr.source)

    return result - parents


def get_variables(ir):
    result = set()
    flt = lambda n: isinstance(n, irast.Parameter)
    result.update(ast.find_children(ir, flt))
    return result


def is_const(ir):
    flt = lambda n: isinstance(n, irast.Set) and n.expr is None
    ir_sets = ast.find_children(ir, flt)
    variables = get_variables(ir)
    return not ir_sets and not variables


def is_aggregated_expr(ir):
    def flt(n):
        if isinstance(n, irast.FunctionCall):
            return n.func.aggregate
        elif isinstance(n, irast.Stmt):
            # Make sure we don't dip into subqueries
            raise ast.SkipNode()

    return bool(set(ast.find_children(ir, flt)))


class LinearPath(list):
    """Denotes a linear path in the graph.

    The path is considered linear if it
    does not have branches and is in the form
    <concept> <link> <concept> <link> ... <concept>
    """

    def __eq__(self, other):
        if not isinstance(other, LinearPath):
            return NotImplemented

        if len(other) != len(self):
            return False
        elif len(self) == 0:
            return True

        if self[0] != other[0]:
            return False

        for i in range(1, len(self) - 1, 2):
            if self[i] != other[i]:
                break
            if self[i + 1] != other[i + 1]:
                break
        else:
            return True
        return False

    def add(self, link, direction, target):
        if not link.generic():
            link = link.bases[0]
        self.append((link, direction))
        self.append(target)

    def rptr(self):
        if len(self) > 1:
            genptr = self[-2][0]
            direction = self[-2][1]
            if direction == s_pointers.PointerDirection.Outbound:
                src = self[-3]
            else:
                src = self[-1]

            if isinstance(src, s_src.Source):
                return src.pointers.get(genptr.name)
            else:
                return None
        else:
            return None

    def rptr_dir(self):
        if len(self) > 1:
            return self[-2][1]
        else:
            return None

    def iter_prefixes(self):
        yield self.__class__(self[:1])

        for i in range(1, len(self) - 1, 2):
            if self[i + 1]:
                yield self.__class__(self[:i + 2])
            else:
                break

    def __hash__(self):
        return hash(tuple(self))

    def __str__(self):
        if not self:
            return ''

        result = f'({self[0].name})'

        for i in range(1, len(self) - 1, 2):
            ptr = self[i][0]
            ptrdir = self[i][1]
            tgt = self[i + 1]

            if tgt:
                lexpr = f'({ptr.name} [TO {tgt.name}])'
            else:
                lexpr = f'({ptr.name})'

            if isinstance(ptr, s_lprops.LinkProperty):
                step = '@'
            else:
                step = f'.{ptrdir}'

            result += f'{step}{lexpr}'

        return result

    __repr__ = __str__


def extend_path(self, schema, source_set, ptr):
    scls = source_set.scls

    if isinstance(ptr, str):
        ptrcls = scls.resolve_pointer(schema, ptr)
    else:
        ptrcls = ptr

    path_id = LinearPath(source_set.path_id)
    path_id.add(ptrcls, s_pointers.PointerDirection.Outbound, ptrcls.target)

    target_set = irast.Set()
    target_set.scls = ptrcls.target
    target_set.path_id = path_id

    ptr = irast.Pointer(
        source=source_set,
        target=target_set,
        ptrcls=ptrcls,
        direction=s_pointers.PointerDirection.Outbound
    )

    target_set.rptr = ptr

    return target_set
