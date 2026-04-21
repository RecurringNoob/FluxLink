#fluxdiff/parser/sexp_parser.py
import re


class Node:
    def __init__(self, name: str = "", values=None, children=None):
        self.name = name
        self.values = values if values is not None else []
        self.children = children if children is not None else []

    def __repr__(self):
        return f"Node({self.name!r}, {self.values!r}, children={len(self.children)})"


# ---------------- TOKENIZER ----------------
def tokenize(text):
    """
    Tokenize S-expression text into atoms, parentheses, and quoted strings.
    """
    token_re = re.compile(
        r'''"([^"\\]*(?:\\.[^"\\]*)*)"   # quoted string
        |(\()                             # open paren
        |(\))                             # close paren
        |([^\s()"]+)                      # unquoted atom
        ''',
        re.VERBOSE,
    )

    for match in token_re.finditer(text):
        quoted, l_paren, r_paren, atom = match.groups()

        if quoted is not None:
            s = quoted.replace("\\\\", "\\")
            s = s.replace('\\"', '"')
            yield s
        elif l_paren:
            yield "("
        elif r_paren:
            yield ")"
        elif atom:
            yield atom


# ---------------- PARSER ----------------
def parse_tokens(tokens):
    """
    Convert token stream into AST (Node tree).

    Fix: raises ValueError on malformed input (unclosed parens, unexpected
    close-parens) instead of silently returning a partial or wrong tree.
    """
    stack = []

    for token in tokens:
        if token == "(":
            stack.append(Node())

        elif token == ")":
            if not stack:
                raise ValueError(
                    "Malformed S-expression: unexpected ')' with empty stack."
                )
            node = stack.pop()
            if stack:
                stack[-1].children.append(node)
            else:
                return node  # root node complete

        else:
            if not stack:
                # Top-level atom outside any list — ignore (e.g. whitespace-only leftovers)
                continue
            cur = stack[-1]
            if not cur.name:
                cur.name = token
            else:
                cur.values.append(token)

    if stack:
        raise ValueError(
            f"Malformed S-expression: {len(stack)} unclosed parenthese(s). "
            f"Top node: {stack[-1]!r}"
        )

    return None  # empty file


def parse_sexp(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()

    result = parse_tokens(tokenize(text))
    if result is None:
        raise ValueError(f"S-expression file appears to be empty: {file_path}")
    return result


# ---------------- INDEXING ----------------
def build_index(root):
    """
    Build name → [nodes] index for O(1) lookups across the whole tree.
    """
    index = {}

    def dfs(node):
        index.setdefault(node.name, []).append(node)
        for child in node.children:
            dfs(child)

    dfs(root)
    return index