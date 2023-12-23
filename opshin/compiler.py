import copy

import logging
from pycardano import PlutusData

from uplc.ast import data_from_cbor
from .optimize.optimize_const_folding import OptimizeConstantFolding
from .optimize.optimize_remove_comments import OptimizeRemoveDeadconstants
from .rewrite.rewrite_augassign import RewriteAugAssign
from .rewrite.rewrite_cast_condition import RewriteConditions
from .rewrite.rewrite_comparison_chaining import RewriteComparisonChaining
from .rewrite.rewrite_forbidden_overwrites import RewriteForbiddenOverwrites
from .rewrite.rewrite_import import RewriteImport
from .rewrite.rewrite_import_dataclasses import RewriteImportDataclasses
from .rewrite.rewrite_import_hashlib import RewriteImportHashlib
from .rewrite.rewrite_import_integrity_check import RewriteImportIntegrityCheck
from .rewrite.rewrite_import_plutusdata import RewriteImportPlutusData
from .rewrite.rewrite_import_typing import RewriteImportTyping
from .rewrite.rewrite_import_uplc_builtins import RewriteImportUPLCBuiltins
from .rewrite.rewrite_inject_builtins import RewriteInjectBuiltins
from .rewrite.rewrite_inject_builtin_constr import RewriteInjectBuiltinsConstr
from .rewrite.rewrite_orig_name import RewriteOrigName
from .rewrite.rewrite_remove_type_stuff import RewriteRemoveTypeStuff
from .rewrite.rewrite_scoping import RewriteScoping
from .rewrite.rewrite_subscript38 import RewriteSubscript38
from .rewrite.rewrite_tuple_assign import RewriteTupleAssign
from .optimize.optimize_remove_pass import OptimizeRemovePass
from .optimize.optimize_remove_deadvars import OptimizeRemoveDeadvars
from .type_inference import *
from .util import (
    CompilingNodeTransformer,
    NoOp,
)
from .typed_ast import (
    transform_ext_params_map,
    transform_output_map,
    RawPlutoExpr,
    PowImpl,
    ByteStrIntMulImpl,
    StrIntMulImpl,
)

_LOGGER = logging.getLogger(__name__)


BoolOpMap = {
    And: plt.And,
    Or: plt.Or,
}

UnaryOpMap = {
    Not: {BoolInstanceType: plt.Not},
    USub: {IntegerInstanceType: lambda x: plt.SubtractInteger(plt.Integer(0), x)},
}


def rec_constant_map_data(c):
    if isinstance(c, bool):
        return uplc.PlutusInteger(int(c))
    if isinstance(c, int):
        return uplc.PlutusInteger(c)
    if isinstance(c, type(None)):
        return uplc.PlutusConstr(0, [])
    if isinstance(c, bytes):
        return uplc.PlutusByteString(c)
    if isinstance(c, str):
        return uplc.PlutusByteString(c.encode())
    if isinstance(c, list):
        return uplc.PlutusList([rec_constant_map_data(ce) for ce in c])
    if isinstance(c, dict):
        return uplc.PlutusMap(
            dict(
                zip(
                    (rec_constant_map_data(ce) for ce in c.keys()),
                    (rec_constant_map_data(ce) for ce in c.values()),
                )
            )
        )
    raise NotImplementedError(f"Unsupported constant type {type(c)}")


def rec_constant_map(c):
    if isinstance(c, bool):
        return uplc.BuiltinBool(c)
    if isinstance(c, int):
        return uplc.BuiltinInteger(c)
    if isinstance(c, type(None)):
        return uplc.BuiltinUnit()
    if isinstance(c, bytes):
        return uplc.BuiltinByteString(c)
    if isinstance(c, str):
        return uplc.BuiltinString(c)
    if isinstance(c, list):
        return uplc.BuiltinList([rec_constant_map(ce) for ce in c])
    if isinstance(c, dict):
        return uplc.BuiltinList(
            [
                uplc.BuiltinPair(*p)
                for p in zip(
                    (rec_constant_map_data(ce) for ce in c.keys()),
                    (rec_constant_map_data(ce) for ce in c.values()),
                )
            ]
        )
    if isinstance(c, PlutusData):
        return data_from_cbor(c.to_cbor())
    raise NotImplementedError(f"Unsupported constant type {type(c)}")


def wrap_validator_double_function(x: plt.AST, pass_through: int = 0):
    """
    Wraps the validator function to enable a double function as minting script

    pass_through defines how many parameters x would normally take and should be passed through to x
    """
    return plt.Lambda(
        [f"v{i}" for i in range(pass_through)] + ["a0", "a1"],
        plt.Let(
            [("p", plt.Apply(x, *(plt.Var(f"v{i}") for i in range(pass_through))))],
            plt.Ite(
                # if the second argument has constructor 0 = script context
                plt.DelayedChooseData(
                    plt.Var("a1"),
                    plt.EqualsInteger(plt.Constructor(plt.Var("a1")), plt.Integer(0)),
                    plt.Bool(False),
                    plt.Bool(False),
                    plt.Bool(False),
                    plt.Bool(False),
                ),
                # call the validator with a0, a1, and plug in "Nothing" for data
                plt.Apply(
                    plt.Var("p"),
                    plt.UPLCConstant(uplc.PlutusConstr(6, [])),
                    plt.Var("a0"),
                    plt.Var("a1"),
                ),
                # else call the validator with a0, a1 and return (now partially bound)
                plt.Apply(plt.Var("p"), plt.Var("a0"), plt.Var("a1")),
            ),
        ),
    )


class NameWriteCollector(CompilingNodeVisitor):
    step = "Collecting variables that are written"

    def __init__(self):
        self.written = defaultdict(int)

    def visit_Name(self, node: Name) -> None:
        if isinstance(node.ctx, Store):
            self.written[node.id] += 1

    def visit_ClassDef(self, node: ClassDef):
        # ignore the content (i.e. attribute names) of class definitions
        self.written[node.name] += 1
        pass

    def visit_FunctionDef(self, node: FunctionDef):
        # ignore the type hints of function arguments
        self.written[node.name] += 1
        for s in node.body:
            self.visit(s)


CallAST = typing.Callable[[plt.AST], plt.AST]


def written_vars(node):
    """
    Returns all variable names written to in this node
    """
    collector = NameWriteCollector()
    collector.visit(node)
    return sorted(collector.written.keys())


class PlutoCompiler(CompilingNodeTransformer):
    """
    Expects a TypedAST and returns UPLC/Pluto like code
    """

    step = "Compiling python statements to UPLC"

    def __init__(self, force_three_params=False, validator_function_name="validator"):
        self.force_three_params = force_three_params
        self.validator_function_name = validator_function_name

    def visit_sequence(self, node_seq: typing.List[typedstmt]) -> CallAST:
        def g(s: plt.AST):
            for n in reversed(node_seq):
                compiled_stmt = self.visit(n)
                s = compiled_stmt(s)
            return s

        return g

    def visit_BinOp(self, node: TypedBinOp) -> plt.AST:
        op = node.left.typ.binop(node.op, node.right)
        return plt.Apply(
            op,
            self.visit(node.left),
            self.visit(node.right),
        )

    def visit_BoolOp(self, node: TypedBoolOp) -> plt.AST:
        op = BoolOpMap.get(type(node.op))
        assert len(node.values) >= 2, "Need to compare at least to values"
        ops = op(
            self.visit(node.values[0]),
            self.visit(node.values[1]),
        )
        for v in node.values[2:]:
            ops = op(ops, self.visit(v))
        return ops

    def visit_UnaryOp(self, node: TypedUnaryOp) -> plt.AST:
        opmap = UnaryOpMap.get(type(node.op))
        assert opmap is not None, f"Operator {type(node.op)} is not supported"
        op = opmap.get(node.operand.typ)
        assert (
            op is not None
        ), f"Operator {type(node.op)} is not supported for type {node.operand.typ}"
        return op(self.visit(node.operand))

    def visit_Compare(self, node: TypedCompare) -> plt.AST:
        assert len(node.ops) == 1, "Only single comparisons are supported"
        assert len(node.comparators) == 1, "Only single comparisons are supported"
        cmpop = node.ops[0]
        comparator = node.comparators[0].typ
        op = node.left.typ.cmp(cmpop, comparator)
        return plt.Apply(
            op,
            self.visit(node.left),
            self.visit(node.comparators[0]),
        )

    def visit_Module(self, node: TypedModule) -> plt.AST:
        # for validators find main function
        # TODO can use more sophisiticated procedure here i.e. functions marked by comment
        main_fun: typing.Optional[InstanceType] = None
        for s in node.body:
            if (
                isinstance(s, FunctionDef)
                and s.orig_name == self.validator_function_name
            ):
                main_fun = s
        assert (
            main_fun is not None
        ), f"Could not find function named {self.validator_function_name}"
        main_fun_typ: FunctionType = main_fun.typ.typ
        assert isinstance(
            main_fun_typ, FunctionType
        ), f"Variable named {self.validator_function_name} is not of type function"

        # check if this is a contract written to double function
        enable_double_func_mint_spend = False
        if len(main_fun_typ.argtyps) >= 3 and self.force_three_params:
            # check if is possible
            second_last_arg = main_fun_typ.argtyps[-2]
            assert isinstance(
                second_last_arg, InstanceType
            ), "Can not pass Class into validator"
            if isinstance(second_last_arg.typ, UnionType):
                possible_types = second_last_arg.typ.typs
            else:
                possible_types = [second_last_arg.typ]
            if any(isinstance(t, UnitType) for t in possible_types):
                _LOGGER.warning(
                    "The redeemer is annotated to be 'None'. This value is usually encoded in PlutusData with constructor id 0 and no fields. If you want the script to double function as minting and spending script, annotate the second argument with 'NoRedeemer'."
                )
            enable_double_func_mint_spend = not any(
                (isinstance(t, RecordType) and t.record.constructor == 0)
                or isinstance(t, UnitType)
                for t in possible_types
            )
            if not enable_double_func_mint_spend:
                _LOGGER.warning(
                    "The second argument to the validator function potentially has constructor id 0. The validator will not be able to double function as minting script and spending script."
                )
        body = node.body + [
            TypedReturn(
                value=Name(
                    id=main_fun.name,
                    typ=InstanceType(main_fun_typ),
                    ctx=Load(),
                ),
                typ=InstanceType(main_fun_typ),
            )
        ]
        written_vs = written_vars(node)

        # write all variables once at the beginning so that we can always access them (only potentially causing a nameerror at runtime)
        validator = plt.Lambda(
            [f"0p{i}" for i, _ in enumerate(main_fun_typ.argtyps)] or ["_"],
            transform_output_map(main_fun_typ.rettyp)(
                plt.Let(
                    [
                        (
                            "0g",
                            plt.Let(
                                [
                                    (
                                        x,
                                        plt.Delay(plt.TraceError(f"NameError: {x}")),
                                    )
                                    for x in written_vs
                                ],
                                self.visit_sequence(body)(
                                    plt.ConstrData(plt.Integer(0), plt.EmptyDataList())
                                ),
                            ),
                        ),
                    ],
                    plt.Apply(
                        plt.Var("0g"),
                        *[
                            plt.Delay(transform_ext_params_map(a)(plt.Var(f"0p{i}")))
                            for i, a in enumerate(main_fun_typ.argtyps)
                        ],
                    ),
                ),
            ),
        )
        if enable_double_func_mint_spend:
            validator = wrap_validator_double_function(
                validator, pass_through=len(main_fun_typ.argtyps) - 3
            )
        elif self.force_three_params:
            # Error if the double function is enforced but not possible
            raise RuntimeError(
                "The contract can not always detect if it was passed three or two parameters on-chain."
            )
        cp = plt.Program((1, 0, 0), validator)
        return cp

    def visit_Constant(self, node: TypedConstant) -> plt.AST:
        if isinstance(node.value, bytes) and node.value != b"":
            try:
                bytes.fromhex(node.value.decode())
            except ValueError:
                pass
            else:
                _LOGGER.warning(
                    f"The string {node.value} looks like it is supposed to be a hex-encoded bytestring but is actually utf8-encoded. Try using `bytes.fromhex('{node.value.decode()}')` instead."
                )
        plt_val = plt.UPLCConstant(rec_constant_map(node.value))
        return plt_val

    def visit_NoneType(self, _: typing.Optional[typing.Any]) -> plt.AST:
        return plt.Unit()

    def visit_Assign(self, node: TypedAssign) -> CallAST:
        assert (
            len(node.targets) == 1
        ), "Assignments to more than one variable not supported yet"
        assert isinstance(
            node.targets[0], Name
        ), "Assignments to other things then names are not supported"
        compiled_e = self.visit(node.value)
        varname = node.targets[0].id
        # first evaluate the term, then wrap in a delay
        return lambda x: plt.Let(
            [(f"0{varname}", compiled_e), (varname, plt.Delay(plt.Var(f"0{varname}")))],
            x,
        )

    def visit_AnnAssign(self, node: AnnAssign) -> CallAST:
        assert isinstance(
            node.target, Name
        ), "Assignments to other things then names are not supported"
        assert isinstance(
            node.target.typ, InstanceType
        ), "Can only assign instances to instances"
        val = self.visit(node.value)
        if isinstance(node.value.typ, InstanceType) and isinstance(
            node.value.typ.typ, AnyType
        ):
            # we need to map this as it will originate from PlutusData
            # AnyType is the only type other than the builtin itself that can be cast to builtin values
            val = transform_ext_params_map(node.target.typ)(val)
        if isinstance(node.target.typ, InstanceType) and isinstance(
            node.target.typ.typ, AnyType
        ):
            # we need to map this back as it will be treated as PlutusData
            # AnyType is the only type other than the builtin itself that can be cast to from builtin values
            val = transform_output_map(node.value.typ)(val)
        return lambda x: plt.Let(
            [
                (f"0{node.target.id}", val),
                (node.target.id, plt.Delay(plt.Var(f"0{node.target.id}"))),
            ],
            x,
        )

    def visit_Name(self, node: TypedName) -> plt.AST:
        # depending on load or store context, return the value of the variable or its name
        if not isinstance(node.ctx, Load):
            raise NotImplementedError(f"Context {node.ctx} not supported")
        if isinstance(node.typ, ClassType):
            # if this is not an instance but a class, call the constructor
            return node.typ.constr()
        return plt.Force(plt.Var(node.id))

    def visit_Expr(self, node: TypedExpr) -> CallAST:
        # we exploit UPLCs eager evaluation here
        # the expression is computed even though its value is eventually discarded
        # Note this really only makes sense for Trace
        # we use an invalid name here to avoid conflicts
        return lambda x: plt.Apply(plt.Lambda(["0"], x), self.visit(node.value))

    def visit_Call(self, node: TypedCall) -> plt.AST:
        # compiled_args = " ".join(f"({self.visit(a)} {STATEMONAD})" for a in node.args)
        # return rf"(\{STATEMONAD} -> ({self.visit(node.func)} {compiled_args})"
        # TODO function is actually not of type polymorphic function type here anymore
        if isinstance(node.func.typ, PolymorphicFunctionInstanceType):
            # edge case for weird builtins that are polymorphic
            func_plt = node.func.typ.polymorphic_function.impl_from_args(
                node.func.typ.typ.argtyps
            )
        else:
            func_plt = self.visit(node.func)
        args = []
        for a, t in zip(node.args, node.func.typ.typ.argtyps):
            assert isinstance(t, InstanceType)
            # pass in all arguments evaluated with the statemonad
            a_int = self.visit(a)
            if isinstance(t.typ, AnyType):
                # if the function expects input of generic type data, wrap data before passing it inside
                a_int = transform_output_map(a.typ)(a_int)
            args.append(a_int)
        # First assign to let to ensure that the arguments are evaluated before the call, but need to delay
        # as this is a variable assignment
        return plt.Let(
            [(f"0p{i}", a) for i, a in enumerate(args)],
            plt.Apply(
                plt.Force(func_plt),
                *[plt.Delay(plt.Var(f"0p{i}")) for i in range(len(args))],
            ),
        )

    def visit_FunctionDef(self, node: TypedFunctionDef) -> CallAST:
        body = node.body.copy()
        # defaults to returning None if there is no return statement
        if node.typ.typ.rettyp.typ == AnyType():
            ret_val = plt.ConstrData(plt.Integer(0), plt.EmptyDataList())
        else:
            ret_val = plt.Unit()
        compiled_body = self.visit_sequence(body)(ret_val)
        return lambda x: plt.Let(
            [
                (
                    node.name,
                    plt.Delay(
                        plt.Lambda(
                            [a.arg for a in node.args.args],
                            compiled_body,
                        )
                    ),
                )
            ],
            x,
        )

    def visit_While(self, node: TypedWhile) -> CallAST:
        # the while loop calls itself, updating the values at overwritten names
        # by overwriting them with arguments to its self-recall
        if node.orelse:
            # If there is orelse, transform it to an appended sequence (TODO check if this is correct)
            cn = copy(node)
            cn.orelse = []
            return self.visit_sequence([cn] + node.orelse)
        compiled_c = self.visit(node.test)
        compiled_s = self.visit_sequence(node.body)
        written_vs = written_vars(node)
        pwritten_vs = [plt.Var(x) for x in written_vs]
        s_fun = lambda x: plt.Lambda(
            ["0while"] + written_vs,
            plt.Ite(
                compiled_c,
                compiled_s(
                    plt.Apply(
                        plt.Var("0while"),
                        plt.Var("0while"),
                        *pwritten_vs,
                    )
                ),
                x,
            ),
        )
        # TODO does this break with a "return" in a loop?
        return lambda x: plt.Let(
            [("0adjusted_next", plt.Lambda(written_vs, x))],
            plt.Apply(
                s_fun(plt.Apply(plt.Var("0adjusted_next"), *pwritten_vs)), *pwritten_vs
            ),
        )

    def visit_For(self, node: TypedFor) -> CallAST:
        if node.orelse:
            # If there is orelse, transform it to an appended sequence (TODO check if this is correct)
            cn = copy(node)
            cn.orelse = []
            return self.visit_sequence([cn] + node.orelse)
        assert isinstance(node.iter.typ, InstanceType)
        if isinstance(node.iter.typ.typ, ListType):
            assert isinstance(
                node.target, Name
            ), "Can only assign value to singleton element"
            compiled_s = self.visit_sequence(node.body)
            compiled_iter = self.visit(node.iter)
            written_vs = written_vars(node)
            scott_monad_update = plt.Lambda(
                ["0f"],
                plt.Apply(plt.Var("0f"), *(plt.Var(x) for x in written_vs)),
            )
            # TODO this will break if a user puts a "return" in a loop
            return lambda x: plt.Let(
                [
                    ("0adjusted_next", plt.Lambda(written_vs, x)),
                    (
                        "0updated_monad",
                        plt.FoldList(
                            compiled_iter,
                            plt.Lambda(
                                ["0state", "0listhead"],
                                plt.Apply(
                                    plt.Var("0state"),
                                    compiled_s(copy.deepcopy(scott_monad_update)),
                                ),
                            ),
                            copy.deepcopy(scott_monad_update),
                        ),
                    ),
                ],
                plt.Apply(plt.Var("0updated_monad"), plt.Var("0adjusted_next")),
            )
        raise NotImplementedError(
            "Compilation of for statements for anything but lists not implemented yet"
        )

    def visit_If(self, node: TypedIf) -> plt.AST:
        written_vs = written_vars(node)
        pwritten_vs = [plt.Var(x) for x in written_vs]
        return lambda x: plt.Let(
            ("0adjusted_next", plt.Lambda(written_vs, x)),
            plt.Ite(
                self.visit(node.test),
                self.visit_sequence(node.body)(
                    plt.Apply(plt.Var("0adjusted_next"), *pwritten_vs)
                ),
                self.visit_sequence(node.orelse)(
                    plt.Apply(plt.Var("0adjusted_next"), *pwritten_vs)
                ),
            ),
        )

    def visit_Return(self, node: TypedReturn) -> CallAST:
        return lambda _: self.visit(node.value)

    def visit_Pass(self, node: TypedPass) -> CallAST:
        return self.visit_sequence([])

    def visit_Subscript(self, node: TypedSubscript) -> plt.AST:
        assert isinstance(
            node.value.typ, InstanceType
        ), "Can only access elements of instances, not classes"
        if isinstance(node.value.typ.typ, TupleType):
            assert isinstance(
                node.slice, Constant
            ), "Only constant index access for tuples is supported"
            assert isinstance(
                node.slice.value, int
            ), "Only constant index integer access for tuples is supported"
            index = node.slice.value
            if index < 0:
                index += len(node.value.typ.typ.typs)
            assert isinstance(node.ctx, Load), "Tuples are read-only"
            return plt.FunctionalTupleAccess(
                self.visit(node.value),
                index,
                len(node.value.typ.typ.typs),
            )
        if isinstance(node.value.typ.typ, PairType):
            assert isinstance(
                node.slice, Constant
            ), "Only constant index access for pairs is supported"
            assert isinstance(
                node.slice.value, int
            ), "Only constant index integer access for pairs is supported"
            index = node.slice.value
            if index < 0:
                index += 2
            assert isinstance(node.ctx, Load), "Pairs are read-only"
            assert (
                0 <= index < 2
            ), f"Pairs only have 2 elements, index should be 0 or 1, is {node.slice.value}"
            member_func = plt.FstPair if index == 0 else plt.SndPair
            # the content of pairs is always Data, so we need to unwrap
            member_typ = node.typ
            return transform_ext_params_map(member_typ)(
                member_func(
                    self.visit(node.value),
                ),
            )
        if isinstance(node.value.typ.typ, ListType):
            if not isinstance(node.slice, Slice):
                assert (
                    node.slice.typ == IntegerInstanceType
                ), "Only single element list index access supported"
                return plt.Let(
                    [
                        (
                            "l",
                            self.visit(node.value),
                        ),
                        (
                            "raw_i",
                            self.visit(node.slice),
                        ),
                        (
                            "i",
                            plt.Ite(
                                plt.LessThanInteger(plt.Var("raw_i"), plt.Integer(0)),
                                plt.AddInteger(
                                    plt.Var("raw_i"), plt.LengthList(plt.Var("l"))
                                ),
                                plt.Var("raw_i"),
                            ),
                        ),
                    ],
                    plt.IndexAccessList(plt.Var("l"), plt.Var("i")),
                )
            else:
                return plt.Let(
                    [
                        (
                            "xs",
                            self.visit(node.value),
                        ),
                        (
                            "raw_i",
                            self.visit(node.slice.lower),
                        ),
                        (
                            "i",
                            plt.Ite(
                                plt.LessThanInteger(plt.Var("raw_i"), plt.Integer(0)),
                                plt.AddInteger(
                                    plt.Var("raw_i"),
                                    plt.LengthList(plt.Var("xs")),
                                ),
                                plt.Var("raw_i"),
                            ),
                        ),
                        (
                            "raw_j",
                            self.visit(node.slice.upper),
                        ),
                        (
                            "j",
                            plt.Ite(
                                plt.LessThanInteger(plt.Var("raw_j"), plt.Integer(0)),
                                plt.AddInteger(
                                    plt.Var("raw_j"),
                                    plt.LengthList(plt.Var("xs")),
                                ),
                                plt.Var("raw_j"),
                            ),
                        ),
                        (
                            "drop",
                            plt.Ite(
                                plt.LessThanEqualsInteger(plt.Var("i"), plt.Integer(0)),
                                plt.Integer(0),
                                plt.Var("i"),
                            ),
                        ),
                        (
                            "take",
                            plt.SubtractInteger(plt.Var("j"), plt.Var("drop")),
                        ),
                    ],
                    plt.Ite(
                        plt.LessThanEqualsInteger(plt.Var("j"), plt.Var("i")),
                        empty_list(node.value.typ.typ.typ),
                        plt.SliceList(
                            plt.Var("drop"),
                            plt.Var("take"),
                            plt.Var("xs"),
                            empty_list(node.value.typ.typ.typ),
                        ),
                    ),
                )
        elif isinstance(node.value.typ.typ, DictType):
            dict_typ = node.value.typ.typ
            if not isinstance(node.slice, Slice):
                return plt.Let(
                    [
                        (
                            "key",
                            self.visit(node.slice),
                        )
                    ],
                    transform_ext_params_map(dict_typ.value_typ)(
                        plt.SndPair(
                            plt.FindList(self.visit(node.value)),
                            plt.Lambda(
                                ["x"],
                                plt.EqualsData(
                                    transform_output_map(dict_typ.key_typ)(
                                        plt.Var("key")
                                    ),
                                    plt.FstPair(plt.Var("x")),
                                ),
                            ),
                            plt.TraceError("KeyError"),
                        ),
                    ),
                )
        elif isinstance(node.value.typ.typ, ByteStringType):
            if not isinstance(node.slice, Slice):
                return plt.Let(
                    [
                        (
                            "bs",
                            self.visit(node.value),
                        ),
                        (
                            "raw_ix",
                            self.visit(node.slice),
                        ),
                        (
                            "ix",
                            plt.Ite(
                                plt.LessThanInteger(plt.Var("raw_ix"), plt.Integer(0)),
                                plt.AddInteger(
                                    plt.Var("raw_ix"),
                                    plt.LengthOfByteString(plt.Var("bs")),
                                ),
                                plt.Var("raw_ix"),
                            ),
                        ),
                    ],
                    plt.IndexByteString(plt.Var("bs"), plt.Var("ix")),
                )
            elif isinstance(node.slice, Slice):
                return plt.Let(
                    [
                        (
                            "bs",
                            self.visit(node.value),
                        ),
                        (
                            "raw_i",
                            self.visit(node.slice.lower),
                        ),
                        (
                            "i",
                            plt.Ite(
                                plt.LessThanInteger(plt.Var("raw_i"), plt.Integer(0)),
                                plt.AddInteger(
                                    plt.Var("raw_i"),
                                    plt.LengthOfByteString(plt.Var("bs")),
                                ),
                                plt.Var("raw_i"),
                            ),
                        ),
                        (
                            "raw_j",
                            self.visit(node.slice.upper),
                        ),
                        (
                            "j",
                            plt.Ite(
                                plt.LessThanInteger(plt.Var("raw_j"), plt.Integer(0)),
                                plt.AddInteger(
                                    plt.Var("raw_j"),
                                    plt.LengthOfByteString(plt.Var("bs")),
                                ),
                                plt.Var("raw_j"),
                            ),
                        ),
                        (
                            "drop",
                            plt.Ite(
                                plt.LessThanEqualsInteger(plt.Var("i"), plt.Integer(0)),
                                plt.Integer(0),
                                plt.Var("i"),
                            ),
                        ),
                        (
                            "take",
                            plt.SubtractInteger(plt.Var("j"), plt.Var("drop")),
                        ),
                    ],
                    plt.Ite(
                        plt.LessThanEqualsInteger(plt.Var("j"), plt.Var("i")),
                        plt.ByteString(b""),
                        plt.SliceByteString(
                            plt.Var("drop"),
                            plt.Var("take"),
                            plt.Var("bs"),
                        ),
                    ),
                )
        raise NotImplementedError(
            f'Could not implement subscript "{node.slice}" of "{node.value}"'
        )

    def visit_Tuple(self, node: TypedTuple) -> plt.AST:
        return plt.FunctionalTuple(*(self.visit(e) for e in node.elts))

    def visit_ClassDef(self, node: TypedClassDef) -> CallAST:
        return lambda x: plt.Let([(node.name, plt.Delay(node.class_typ.constr()))], x)

    def visit_Attribute(self, node: TypedAttribute) -> plt.AST:
        assert isinstance(
            node.typ, InstanceType
        ), "Can only access attributes of instances"
        obj = self.visit(node.value)
        attr = node.value.typ.attribute(node.attr)
        return plt.Apply(attr, obj)

    def visit_Assert(self, node: TypedAssert) -> CallAST:
        return lambda x: plt.Ite(
            self.visit(node.test),
            x,
            plt.Apply(
                plt.Error(),
                plt.Trace(self.visit(node.msg), plt.Unit())
                if node.msg is not None
                else plt.Unit(),
            ),
        )

    def visit_RawPlutoExpr(self, node: RawPlutoExpr) -> plt.AST:
        return node.expr

    def visit_List(self, node: TypedList) -> plt.AST:
        assert isinstance(node.typ, InstanceType)
        assert isinstance(node.typ.typ, ListType)
        l = empty_list(node.typ.typ.typ)
        for e in reversed(node.elts):
            l = plt.MkCons(self.visit(e), l)
        return l

    def visit_Dict(self, node: TypedDict) -> plt.AST:
        assert isinstance(node.typ, InstanceType)
        assert isinstance(node.typ.typ, DictType)
        key_type = node.typ.typ.key_typ
        value_type = node.typ.typ.value_typ
        l = plt.EmptyDataPairList()
        for k, v in zip(node.keys, node.values):
            l = plt.MkCons(
                plt.MkPairData(
                    transform_output_map(key_type)(
                        self.visit(k),
                    ),
                    transform_output_map(value_type)(
                        self.visit(v),
                    ),
                ),
                l,
            )
        return l

    def visit_IfExp(self, node: TypedIfExp) -> plt.AST:
        return plt.Ite(
            self.visit(node.test),
            self.visit(node.body),
            self.visit(node.orelse),
        )

    def visit_ListComp(self, node: TypedListComp) -> plt.AST:
        assert len(node.generators) == 1, "Currently only one generator supported"
        gen = node.generators[0]
        assert isinstance(gen.iter.typ, InstanceType), "Only lists are valid generators"
        assert isinstance(gen.iter.typ.typ, ListType), "Only lists are valid generators"
        assert isinstance(
            gen.target, Name
        ), "Can only assign value to singleton element"
        lst = self.visit(gen.iter)
        ifs = None
        for ifexpr in gen.ifs:
            if ifs is None:
                ifs = self.visit(ifexpr)
            else:
                ifs = plt.And(ifs, self.visit(ifexpr))
        map_fun = plt.Lambda(
            ["0x"],
            plt.Let(
                [(gen.target.id, plt.Var("0x"))],
                self.visit(node.elt),
            ),
        )
        empty_list_con = empty_list(node.elt.typ)
        if ifs is not None:
            filter_fun = plt.Lambda(
                ["0x"],
                plt.Let(
                    [(gen.target.id, plt.Var("0x"))],
                    ifs,
                ),
            )
            return plt.MapFilterList(
                lst,
                filter_fun,
                map_fun,
                empty_list_con,
            )
        else:
            return plt.MapList(
                lst,
                map_fun,
                empty_list_con,
            )

    def visit_FormattedValue(self, node: TypedFormattedValue) -> plt.AST:
        return plt.Apply(
            node.value.typ.stringify(),
            self.visit(node.value),
        )

    def visit_JoinedStr(self, node: TypedJoinedStr) -> plt.AST:
        joined_str = plt.Text("")
        for v in reversed(node.values):
            joined_str = plt.AppendString(self.visit(v), joined_str)
        return joined_str

    def generic_visit(self, node: AST) -> plt.AST:
        raise NotImplementedError(f"Can not compile {node}")


def compile(
    prog: AST,
    filename=None,
    force_three_params=False,
    remove_dead_code=True,
    constant_folding=False,
    validator_function_name="validator",
    allow_isinstance_anything=False,
) -> plt.Program:
    compile_pipeline = [
        # Important to call this one first - it imports all further files
        RewriteImport(filename=filename),
        # Rewrites that simplify the python code
        OptimizeConstantFolding() if constant_folding else NoOp(),
        RewriteSubscript38(),
        RewriteAugAssign(),
        RewriteComparisonChaining(),
        RewriteTupleAssign(),
        RewriteImportIntegrityCheck(),
        RewriteImportPlutusData(),
        RewriteImportHashlib(),
        RewriteImportTyping(),
        RewriteForbiddenOverwrites(),
        RewriteImportDataclasses(),
        RewriteInjectBuiltins(),
        RewriteConditions(),
        # The type inference needs to be run after complex python operations were rewritten
        AggressiveTypeInferencer(allow_isinstance_anything),
        # Rewrites that circumvent the type inference or use its results
        RewriteImportUPLCBuiltins(),
        RewriteInjectBuiltinsConstr(),
        RewriteRemoveTypeStuff(),
        # Save the original names of variables
        RewriteOrigName(),
        RewriteScoping(),
        # Apply optimizations
        OptimizeRemoveDeadvars() if remove_dead_code else NoOp(),
        OptimizeRemoveDeadconstants(),
        OptimizeRemovePass(),
    ]
    for s in compile_pipeline:
        prog = s.visit(prog)
        prog = custom_fix_missing_locations(prog)

    # the compiler runs last
    s = PlutoCompiler(
        force_three_params=force_three_params,
        validator_function_name=validator_function_name,
    )
    prog = s.visit(prog)

    return prog
