"""A QR encoder, because pairing a phone by typing a URL is a bad time.

Carraway ships with no runtime dependencies, and this is the one place a
picture is genuinely better than text: the alternative is reading sixteen
random characters off a laptop screen and typing them into a phone keyboard.

Scope is deliberately narrow — byte mode, error correction level M, versions
1 to 10, which covers any pairing URL with room to spare. A general encoder
would be several times this size and none of it would ever run.

Correctness is not taken on faith: `tests/test_qr.py` compares the output
module for module against a reference implementation across a range of
inputs and lengths. Getting this subtly wrong produces a code that looks
perfectly fine and does not scan, which is not a thing to discover in a shop.

The algorithm is ISO/IEC 18004. The steps, in the order they appear below:
encode the data, add error correction, interleave the blocks, lay the result
out around the finder patterns, then pick the mask that scores best.
"""

from __future__ import annotations

# Data capacity in bytes, and error-correction structure, for level M.
# (version) -> (data codewords, ec codewords per block, blocks in group 1,
#               data codewords per block in group 1, blocks in group 2,
#               data codewords per block in group 2)
_LEVEL_M: dict[int, tuple[int, int, int, int, int, int]] = {
    1: (16, 10, 1, 16, 0, 0),
    2: (28, 16, 1, 28, 0, 0),
    3: (44, 26, 1, 44, 0, 0),
    4: (64, 18, 2, 32, 0, 0),
    5: (86, 24, 2, 43, 0, 0),
    6: (108, 16, 4, 27, 0, 0),
    7: (124, 18, 4, 31, 0, 0),
    8: (154, 22, 2, 38, 2, 39),
    9: (182, 22, 3, 36, 2, 37),
    10: (216, 26, 4, 43, 1, 44),
}

# Where the alignment patterns go, per version. Version 1 has none.
_ALIGNMENT: dict[int, list[int]] = {
    1: [],
    2: [6, 18],
    3: [6, 22],
    4: [6, 26],
    5: [6, 30],
    6: [6, 34],
    7: [6, 22, 38],
    8: [6, 24, 42],
    9: [6, 26, 46],
    10: [6, 28, 50],
}

# Format information for level M, one entry per mask, already masked with
# the 0x5412 constant the spec requires.
_FORMAT_M: list[int] = [
    0x5412,
    0x5125,
    0x5E7C,
    0x5B4B,
    0x45F9,
    0x40CE,
    0x4F97,
    0x4AA0,
]

# Version 7 and up carry an 18-bit version block twice, beside the top-right
# and bottom-left finders. Leaving it out produces a code that looks perfectly
# correct and that no scanner will read — which is exactly what happened, and
# what the round-trip test caught. Versions 1 to 6 have no such block.
_VERSION_INFO: dict[int, int] = {
    7: 0x07C94,
    8: 0x085BC,
    9: 0x09A99,
    10: 0x0A4D3,
}

_PAD_BYTES = (0xEC, 0x11)


class QRError(ValueError):
    """The data will not fit in the versions this encoder supports."""


# -- GF(256) arithmetic for Reed-Solomon ----------------------------------

_EXP: list[int] = [0] * 512
_LOG: list[int] = [0] * 256


def _build_tables() -> None:
    """Log and antilog tables for GF(256) with the QR primitive 0x11D."""
    value = 1
    for power in range(255):
        _EXP[power] = value
        _LOG[value] = power
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for power in range(255, 512):
        _EXP[power] = _EXP[power - 255]


_build_tables()


def _mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    """The generator polynomial for `degree` error-correction codewords."""
    poly = [1]
    for i in range(degree):
        nxt = [0] * (len(poly) + 1)
        for j, coeff in enumerate(poly):
            nxt[j] ^= coeff
            nxt[j + 1] ^= _mul(coeff, _EXP[i])
        poly = nxt
    return poly


def _ec_codewords(data: list[int], count: int) -> list[int]:
    """Reed-Solomon remainder: the error correction for one block."""
    gen = _generator(count)
    remainder = list(data) + [0] * count
    for i in range(len(data)):
        factor = remainder[i]
        if factor:
            for j, coeff in enumerate(gen):
                remainder[i + j] ^= _mul(coeff, factor)
    return remainder[len(data) :]


# -- encoding --------------------------------------------------------------


def _choose_version(length: int) -> int:
    for version in sorted(_LEVEL_M):
        # 4 bits of mode, 8 or 16 bits of length, then the data itself.
        header = 4 + (8 if version < 10 else 16)
        if (header + length * 8 + 7) // 8 <= _LEVEL_M[version][0]:
            return version
    raise QRError(
        f"{length} bytes is more than this encoder handles; "
        "it goes up to version 10 at error correction level M."
    )


def _bitstream(data: bytes, version: int) -> list[int]:
    """Mode, length, payload, terminator, padding — as a list of codewords."""
    capacity = _LEVEL_M[version][0]
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        for shift in range(width - 1, -1, -1):
            bits.append((value >> shift) & 1)

    put(0b0100, 4)  # byte mode
    put(len(data), 8 if version < 10 else 16)
    for byte in data:
        put(byte, 8)

    # Terminator, then pad to a byte boundary.
    put(0, min(4, capacity * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)

    codewords = [int("".join(map(str, bits[i : i + 8])), 2) for i in range(0, len(bits), 8)]
    # Pad bytes alternate, and the first one is always 0xEC. Counting from
    # the number of codewords already present instead gets that backwards
    # whenever the data and the capacity happen to share a parity, which is
    # a wrong symbol that still looks like a QR code.
    for index in range(capacity - len(codewords)):
        codewords.append(_PAD_BYTES[index % 2])
    return codewords[:capacity]


def _interleave(codewords: list[int], version: int) -> list[int]:
    """Split into blocks, add error correction, then interleave both."""
    _, ec_len, g1_blocks, g1_size, g2_blocks, g2_size = _LEVEL_M[version]

    blocks: list[list[int]] = []
    at = 0
    for _ in range(g1_blocks):
        blocks.append(codewords[at : at + g1_size])
        at += g1_size
    for _ in range(g2_blocks):
        blocks.append(codewords[at : at + g2_size])
        at += g2_size

    ec_blocks = [_ec_codewords(block, ec_len) for block in blocks]

    out: list[int] = []
    for i in range(max(len(b) for b in blocks)):
        for block in blocks:
            if i < len(block):
                out.append(block[i])
    for i in range(ec_len):
        for block in ec_blocks:
            out.append(block[i])
    return out


# -- laying it out ---------------------------------------------------------


def _blank(size: int) -> tuple[list[list[int | None]], list[list[bool]]]:
    return [[None] * size for _ in range(size)], [[False] * size for _ in range(size)]


def _place_fixed(grid, reserved, version: int) -> None:
    size = len(grid)

    def finder(row: int, col: int) -> None:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = row + dr, col + dc
                if not (0 <= r < size and 0 <= c < size):
                    continue
                inside = 0 <= dr <= 6 and 0 <= dc <= 6
                ring = dr in (0, 6) or dc in (0, 6)
                core = 2 <= dr <= 4 and 2 <= dc <= 4
                grid[r][c] = 1 if inside and (ring or core) else 0
                reserved[r][c] = True

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for centre_r in _ALIGNMENT[version]:
        for centre_c in _ALIGNMENT[version]:
            if reserved[centre_r][centre_c]:
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    r, c = centre_r + dr, centre_c + dc
                    grid[r][c] = 1 if max(abs(dr), abs(dc)) != 1 else 0
                    reserved[r][c] = True

    for i in range(8, size - 8):
        bit = 1 - (i % 2)
        if not reserved[6][i]:
            grid[6][i] = bit
            reserved[6][i] = True
        if not reserved[i][6]:
            grid[i][6] = bit
            reserved[i][6] = True

    # The version block, for the versions that have one. Reserved and written
    # here rather than later, so laying out the data cannot walk over it.
    if version in _VERSION_INFO:
        for i in range(18):
            bit = (_VERSION_INFO[version] >> i) & 1
            row, col = size - 11 + i % 3, i // 3
            grid[row][col] = bit
            reserved[row][col] = True
            # The same eighteen bits again, transposed, beside the other finder.
            grid[col][row] = bit
            reserved[col][row] = True

    # The dark module, always set, and the format areas around the finders.
    grid[size - 8][8] = 1
    reserved[size - 8][8] = True
    for i in range(9):
        for r, c in ((8, i), (i, 8)):
            if 0 <= r < size and 0 <= c < size and not reserved[r][c]:
                reserved[r][c] = True
    for i in range(8):
        for r, c in ((8, size - 1 - i), (size - 1 - i, 8)):
            if not reserved[r][c]:
                reserved[r][c] = True


def _place_data(grid, reserved, bits: list[int]) -> None:
    """Fill the free modules in the spec's upward-then-downward zig-zag."""
    size = len(grid)
    index = 0
    upward = True
    col = size - 1
    while col > 0:
        if col == 6:  # the vertical timing pattern is not a data column
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if reserved[row][c]:
                    continue
                grid[row][c] = bits[index] if index < len(bits) else 0
                index += 1
        upward = not upward
        col -= 2


def _mask(row: int, col: int, pattern: int) -> bool:
    return (
        (row + col) % 2 == 0,
        row % 2 == 0,
        col % 3 == 0,
        (row + col) % 3 == 0,
        (row // 2 + col // 3) % 2 == 0,
        (row * col) % 2 + (row * col) % 3 == 0,
        ((row * col) % 2 + (row * col) % 3) % 2 == 0,
        ((row + col) % 2 + (row * col) % 3) % 2 == 0,
    )[pattern]


def _penalty(grid: list[list[int]]) -> int:
    """The spec's four penalty rules, used to pick a mask."""
    size = len(grid)
    score = 0

    for line in list(grid) + [list(col) for col in zip(*grid, strict=True)]:
        run, previous = 1, line[0]
        for cell in line[1:]:
            if cell == previous:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run, previous = 1, cell
        if run >= 5:
            score += 3 + (run - 5)

    for r in range(size - 1):
        for c in range(size - 1):
            block = grid[r][c] + grid[r][c + 1] + grid[r + 1][c] + grid[r + 1][c + 1]
            if block in (0, 4):
                score += 3

    finder = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    for line in list(grid) + [list(col) for col in zip(*grid, strict=True)]:
        for i in range(size - 10):
            window = line[i : i + 11]
            if window == finder or window == finder[::-1]:
                score += 40

    # Rule 4: how far the proportion of dark modules strays from half. The
    # spec takes the two multiples of five either side of the actual ratio and
    # uses whichever is closer to 50 — not the ratio itself, which is what a
    # rough version of this got wrong and why the worst mask kept winning.
    dark = sum(sum(row) for row in grid)
    ratio = dark * 100 / (size * size)
    lower = int(ratio // 5) * 5
    upper = lower + 5
    score += 10 * min(abs(lower - 50) // 5, abs(upper - 50) // 5)
    return score


def encode(text: str, mask: int | None = None) -> list[list[int]]:
    """The QR modules for `text`, as rows of 0 and 1. No quiet zone.

    >>> len(encode("hello"))
    21
    """
    data = text.encode("utf-8")
    version = _choose_version(len(data))
    size = version * 4 + 17

    codewords = _interleave(_bitstream(data, version), version)
    bits = [(word >> shift) & 1 for word in codewords for shift in range(7, -1, -1)]

    grid, reserved = _blank(size)
    _place_fixed(grid, reserved, version)
    _place_data(grid, reserved, bits)

    best: tuple[int, list[list[int]]] | None = None
    # `mask` is for tests that need to pin one down; normally all eight are
    # scored and the best wins, which is what the spec asks for.
    for pattern in range(8) if mask is None else (mask,):
        candidate = [
            [
                cell ^ 1
                if not reserved[r][c] and _mask(r, c, pattern)
                else (cell if cell is not None else 0)
                for c, cell in enumerate(row)
            ]
            for r, row in enumerate(grid)
        ]
        _write_format(candidate, pattern)
        score = _penalty(candidate)
        if best is None or score < best[0]:
            best = (score, candidate)

    assert best is not None
    return best[1]


def _write_format(grid: list[list[int]], pattern: int) -> None:
    size = len(grid)
    bits = [(_FORMAT_M[pattern] >> i) & 1 for i in range(14, -1, -1)]

    for i in range(6):
        grid[8][i] = bits[i]
    grid[8][7] = bits[6]
    grid[8][8] = bits[7]
    grid[7][8] = bits[8]
    for i in range(9, 15):
        grid[14 - i][8] = bits[i]

    # The second copy: seven bits up the left edge, then eight along the top
    # right. The split is 7/8 rather than 8/7 — putting eight down the left
    # lands the eighth on the dark module, which is then overwritten, so that
    # bit is simply never written anywhere and one module comes out wrong.
    for i in range(7):
        grid[size - 1 - i][8] = bits[i]
    for i in range(7, 15):
        grid[8][size - 15 + i] = bits[i]
    grid[size - 8][8] = 1
