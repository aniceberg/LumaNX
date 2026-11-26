"""The registered Mario Kart World NUP update and Nextendo's patch-free build."""
from core import Failure

TITLE = '0400C3F00006E000'
INTERNAL = 'v917504'
RETAIL = '1.8.0'
MAIN = 'd00e4f399e411d0613aa8d26b1c02fef'
FULL_MAIN = bytes.fromhex(MAIN + '0' * 32)


def update_selected(game):
    return game.get('id') == TITLE and game.get('sourceKind') == 'nup' and any(
        p['type'] == 'update' and p['version'] == int(INTERNAL[1:]) for p in game['packages'])


def verify_main(path):
    with path.open('rb') as source:
        header = source.read(0x60)
    if header[:4] != b'NSO0' or header[0x40:0x60] != FULL_MAIN:
        raise Failure('Mario Kart World 的 1.8.0 构建标识不匹配。')
