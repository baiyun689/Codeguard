"""context_provider AST 接入单测。"""

from codeguard_agent.pipeline.context.provider import _split_ast_blocks


def test_split_ast_blocks_single_file():
    text = "AST for: Foo.java\n  class: Foo\n    Methods:\n      void bar()"
    blocks = _split_ast_blocks(text)
    assert len(blocks) == 1
    assert blocks[0].startswith("AST for: Foo.java")


def test_split_ast_blocks_multiple_files():
    text = "AST for: Foo.java\n  class: Foo\n\nAST for: Bar.java\n  class: Bar"
    blocks = _split_ast_blocks(text)
    assert len(blocks) == 2
    assert "Foo.java" in blocks[0]
    assert "Bar.java" in blocks[1]


def test_split_ast_blocks_empty():
    assert _split_ast_blocks("") == []
    assert _split_ast_blocks("   ") == []


def test_split_ast_blocks_no_header():
    """无 AST for 头时返回整个文本作为一个块。"""
    text = "some other content"
    blocks = _split_ast_blocks(text)
    assert len(blocks) == 1
    assert blocks[0] == text
