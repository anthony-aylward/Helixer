import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        '--helixer-model-path',
        action='store',
        default=None,
        metavar='PATH',
        help=(
            'Path to a Helixer model .h5 file used in inference tests. '
            'When omitted, the land_plant model is used from the default Helixer '
            'model directory (~/.local/share/Helixer/models/land_plant on Linux).'
        ),
    )
