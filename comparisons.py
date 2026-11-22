"""Recognize equivalent comparisons for typed fields in the pinned test corpus."""
import ast
import re


def identical(before, after):
    """Compare AST structure without creating large intermediate dump strings."""
    pending = [(before, after)]
    while pending:
        left, right = pending.pop()
        if type(left) is not type(right):
            return False
        if isinstance(left, ast.AST):
            pending.extend((getattr(left, name, None), getattr(right, name, None))
                           for name in left._fields)
        elif isinstance(left, list):
            if len(left) != len(right):
                return False
            pending.extend(zip(left, right))
        elif left != right:
            return False
    return True


def expression(node):
    return ast.unparse(node)


def assignments(tree, target):
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(expression(item) == target for item in node.targets):
            yield node.value
        if isinstance(node, ast.AnnAssign) and expression(node.target) == target:
            yield node.value


def plain_class(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            if not node.keywords and not node.decorator_list and not any(
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name in {"__eq__", "__ne__"} for item in node.body
            ):
                return node
    return None


class ClosedWorldComparisons:
    """Use the declared concrete types of fields and in-repository enum assignments."""
    def __init__(self, checkout, tree, corpus=None):
        self.checkout = checkout
        self.tree = tree
        self.cache = {}
        self.corpus = corpus

    def for_name(self, name):
        if name not in self.cache:
            pattern = re.compile(r"\b" + re.escape(name) + r"\b")
            trees = []
            if self.corpus is not None:
                trees = [tree for content, tree in self.corpus.values() if pattern.search(content)]
            else:
                for folder in ("sqlglot", "tests"):
                    for path in (self.checkout / folder).rglob("*.py"):
                        content = path.read_text(encoding="utf-8")
                        if pattern.search(content):
                            trees.append(ast.parse(content))
            self.cache[name] = trees
        return self.cache[name]

    def directly_assigned(self, field):
        for tree in self.for_name(field):
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and node.attr == field
                        and (isinstance(node.ctx, ast.Del) or
                             isinstance(node.ctx, ast.Store) and expression(node.value) != "self")):
                    return False
                if (isinstance(node, ast.Call) and expression(node.func) in {"setattr", "delattr"}
                        and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value == field):
                    return False
        return True

    def same_enum(self, field, right):
        if not isinstance(right, ast.Attribute) or not isinstance(right.value, ast.Name):
            return False
        enum = right.value.id
        definition = plain_class(self.tree, enum)
        if definition is None:
            return False
        bases = {expression(base) for base in definition.bases}
        if not bases <= {"str", "int", "Enum", "AutoName"} or not bases & {"Enum", "AutoName"}:
            return False
        if "AutoName" in bases:
            helper = ast.parse((self.checkout / "sqlglot/helper.py").read_text(encoding="utf-8"))
            auto = plain_class(helper, "AutoName")
            if auto is None or [expression(base) for base in auto.bases] != ["Enum"]:
                return False
        if not list(assignments(definition, right.attr)):
            return False
        values = [value for tree in self.for_name(field) for value in assignments(tree, f"self.{field}")]
        if not values:
            return False
        for value in values:
            if isinstance(value, ast.Call) and expression(value.func) == enum:
                continue
            if not isinstance(value, ast.Attribute) or expression(value.value) != "self":
                return False
            backing = value.attr
            stores = []
            for tree in self.for_name(backing):
                stores.extend(assignments(tree, backing))
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store) and node.attr == backing:
                        stores.extend(assignments(tree, expression(node)))
            if not stores or not all(isinstance(item, ast.Attribute) and expression(item.value) == enum
                                     for item in stores):
                return False
        return True

    def object_or_none(self, field):
        declarations = [node.annotation for node in ast.walk(self.tree)
                        if isinstance(node, ast.AnnAssign) and expression(node.target) == f"self.{field}"]
        if len(declarations) != 1:
            return False
        annotation = declarations[0]
        if not isinstance(annotation, ast.BinOp) or not isinstance(annotation.op, ast.BitOr):
            return False
        if not isinstance(annotation.left, ast.Name) or expression(annotation.right) != "None":
            return False
        name = annotation.left.id
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                continue
            if not any(item.name == name and not item.asname for item in node.names):
                continue
            location = self.checkout / (node.module.replace(".", "/") + ".py")
            if not location.is_file():
                continue
            definition = plain_class(ast.parse(location.read_text(encoding="utf-8")), name)
            if definition is None or definition.bases:
                continue
            return not any(
                isinstance(subclass, ast.ClassDef) and any(
                    expression(base).split(".")[-1] == name for base in subclass.bases
                )
                for tree in self.for_name(name) for subclass in ast.walk(tree)
            )
        return False

    def normalize(self, node):
        if (len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Eq, ast.NotEq))
                or not isinstance(node.left, ast.Attribute) or expression(node.left.value) != "self"):
            return node
        other, = node.comparators
        field = node.left.attr
        if (expression(other) == "None" and self.object_or_none(field)
                or self.same_enum(field, other)) and self.directly_assigned(field):
            node.ops[0] = ast.Is() if isinstance(node.ops[0], ast.Eq) else ast.IsNot()
        return node


def equivalent_comparison(checkout, old, new, proven=None):
    """Require that every AST difference vanish after an identity comparison proof."""
    old_nodes = list(ast.walk(old))
    new_nodes = list(ast.walk(new))
    if len(old_nodes) != len(new_nodes):
        return False
    changes = [(a, b) for a, b in zip(old_nodes, new_nodes) if type(a) is not type(b)]
    if not changes or not all(
        {type(a), type(b)} in ({ast.Eq, ast.Is}, {ast.NotEq, ast.IsNot}) for a, b in changes
    ):
        return False

    # Inspect only changed comparisons. Other equality expressions are left untouched.
    pairs = [(a, b) for a, b in zip(old_nodes, new_nodes)
             if isinstance(a, ast.Compare) and isinstance(b, ast.Compare)
             and [type(op) for op in a.ops] != [type(op) for op in b.ops]]
    checker = ClosedWorldComparisons(checkout, old)
    originals = [(node, node.ops.copy()) for pair in pairs for node in pair]
    try:
        for before, after in pairs:
            if proven is None:
                checker.normalize(before)
                checker.normalize(after)
            else:
                for node in (before, after):
                    if ast.unparse(node) in proven:
                        node.ops = [ast.Is() if isinstance(node.ops[0], (ast.Eq, ast.Is))
                                    else ast.IsNot()]
        return identical(old, new)
    finally:
        for node, ops in originals:
            node.ops = ops


def comparison_proofs(checkout, tree, corpus):
    """Record comparison equivalences derived exclusively from the pinned source."""
    checker = ClosedWorldComparisons(checkout, tree, corpus)
    proven = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.ops[0], (ast.Is, ast.IsNot))
                and isinstance(node.left, ast.Attribute)
                and isinstance(node.left.value, ast.Name) and node.left.value.id == "self"):
            continue
        original_ops = node.ops
        node.ops = [ast.Eq() if isinstance(original_ops[0], ast.Is) else ast.NotEq()]
        equality = ast.unparse(node)
        checker.normalize(node)
        if isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            proven.extend((equality, ast.unparse(node)))
        node.ops = original_ops
    return proven
