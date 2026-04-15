from quasseltui import __version__
from quasseltui.cli import build_parser


def test_version_is_string() -> None:
    assert isinstance(__version__, str)


def test_parser_builds() -> None:
    parser = build_parser()
    args = parser.parse_args([])
    assert args is not None
