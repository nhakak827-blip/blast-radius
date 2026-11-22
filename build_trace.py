"""Observe configuration reads while building coverage from the pinned source."""
import ast
from collections import defaultdict
import importlib.machinery
from pathlib import Path
import sys

reads = defaultdict(set)
context = ''
checkout = None
owner_paths = {}


class ConfigDict(dict):
    def __init__(self, path, name, values):
        super().__init__(values)
        self.path = path
        self.name = name

    def get(self, key, default=None):
        reads[(self.path, self.name, str(key))].add(context)
        return super().get(key, default)


def config_dict(path, name, values):
    return ConfigDict(path, name, values)


def read_attr(receiver, name):
    value = getattr(receiver, name)
    klass = receiver if isinstance(receiver, type) else type(receiver)
    for owner in klass.__mro__:
        if name in vars(owner):
            if owner not in owner_paths:
                module = sys.modules.get(owner.__module__)
                filename = getattr(module, '__file__', None)
                resolved = Path(filename).resolve() if filename else None
                owner_paths[owner] = (resolved.relative_to(checkout).as_posix()
                                      if resolved and resolved.is_relative_to(checkout) else None)
            path = owner_paths[owner]
            if path is not None:
                reads[(path, name, '')].add(context)
            break
    return value


def helper(name, args, original):
    module = ast.Call(func=ast.Name(id='__import__', ctx=ast.Load()),
                      args=[ast.Constant(value='build_trace')], keywords=[])
    call = ast.Call(func=ast.Attribute(value=module, attr=name, ctx=ast.Load()),
                    args=args, keywords=[])
    return ast.copy_location(call, original)


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Assign(self, node):
        self.generic_visit(node)
        if (len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'NUMERIC_ESCAPES' and isinstance(node.value, ast.Dict)):
            node.value = helper('config_dict', [ast.Constant(value=self.path),
                                ast.Constant(value='NUMERIC_ESCAPES'), node.value], node.value)
        return node

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        if (isinstance(node.target, ast.Name) and node.target.id == 'NUMERIC_ESCAPES'
                and isinstance(node.value, ast.Dict)):
            node.value = helper('config_dict', [ast.Constant(value=self.path),
                                ast.Constant(value='NUMERIC_ESCAPES'), node.value], node.value)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr == 'WEEK_OFFSET':
            return helper('read_attr', [node.value, ast.Constant(value=node.attr)], node)
        return node


def install(root):
    global checkout
    checkout = root
    original = importlib.machinery.SourceFileLoader.get_code

    def get_code(loader, fullname):
        filename = Path(loader.path).resolve()
        if not filename.is_relative_to(root / 'sqlglot') or filename.suffix != '.py':
            return original(loader, fullname)
        source = filename.read_text(encoding='utf-8')
        if 'NUMERIC_ESCAPES' not in source and 'WEEK_OFFSET' not in source:
            return original(loader, fullname)
        tree = ast.parse(source, filename=str(filename))
        tree = Instrument(filename.relative_to(root).as_posix()).visit(tree)
        return compile(ast.fix_missing_locations(tree), str(filename), 'exec', dont_inherit=True)

    importlib.machinery.SourceFileLoader.get_code = get_code
